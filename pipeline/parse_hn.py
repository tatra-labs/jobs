"""
parse_hn.py -- parse the cached "Ask HN: Who is hiring?" threads into a posting-level
corpus and browser-ready monthly/yearly trend aggregates.

Inputs   raw/hn/_threads.json + raw/hn/{thread_id}.json   (written by pipeline/fetch_hn.py)
Outputs  data/hn_postings.jsonl   one compact JSON object per job posting
         data/hn_trends.json     monthly + yearly aggregates for the site
         data/hn_taxonomy.json    the technology + role taxonomies actually used

Design notes
------------
* One TOP-LEVEL comment == one job posting. Replies (grandchildren) are ignored.
* The de-facto header format is  Company | Role | Location | Type | Salary | URL  but
  adherence is loose, so every extractor degrades gracefully and records "unknown"
  rather than guessing.
* Remote classification runs on the HEADER first (that is where the location field is),
  then on a wider header zone, then on the body, and stops at the first tier that fires.
  Negations ("no remote", "onsite only") are checked before positives at every tier.
* Ambiguous technology tokens (Go, R, C, Rust, Swift, Dart, Spark, Ruby, Phoenix, Nomad,
  Lambda, Express, Spring, Unity, Julia, Nim, Zig, Tailwind, RAG, ...) are only accepted
  when (a) no anti-pattern overlaps the match and (b) an unambiguous technology token or a
  stack-qualifier word occurs within CONTEXT_WINDOW characters.

Usage
  uv run python pipeline/parse_hn.py            # full run
  uv run python pipeline/parse_hn.py --audit    # + print ambiguous-token match samples
"""

import argparse
import html
import json
import multiprocessing
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA, RAW, read_json, write_json  # noqa: E402

HN_RAW = RAW / "hn"
THREADS = HN_RAW / "_threads.json"
OUT_POSTINGS = DATA / "hn_postings.jsonl"
OUT_TRENDS = DATA / "hn_trends.json"
OUT_TAXONOMY = DATA / "hn_taxonomy.json"

EXCERPT_CHARS = 400       # excerpt kept per posting (the ladder below may shorten it)
EXCERPT_STEPS = [400, 320, 260, 200, 160, 120, 80, 0]
MAX_JSONL_BYTES = 40_000_000
CONTEXT_WINDOW = 90       # chars either side of an ambiguous token that may confirm it
REQUIRE_WINDOW = 250      # wider window for a technology's mandatory-context regex

# ---------------------------------------------------------------------------
# 1. text cleaning
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://\S+|\bwww\.[^\s,;)]+", re.I)


def clean_html(s):
    """HN comment HTML -> plain text. Keeps anchor text, turns <p> into blank lines."""
    if not s:
        return ""
    s = re.sub(r"(?is)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?is)<\s*/?\s*p\s*/?\s*>", "\n", s)
    s = re.sub(r"(?is)<a\b[^>]*>(.*?)</a>", r"\1", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("\u00a0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def strip_urls(s):
    """Remove URLs before technology matching (github.com/rust-lang etc. are not stacks)."""
    return URL_RE.sub(" ", s)


# ---------------------------------------------------------------------------
# 2. technology taxonomy
# ---------------------------------------------------------------------------
# Each entry: (tech_id, label, category, [patterns], [anti-patterns], ambiguous?)
# Patterns are matched case-INSENSITIVELY unless they contain "(?-i:...)".
# Anti-patterns are matched case-insensitively; an ambiguous hit whose span overlaps an
# anti-pattern hit is discarded.

_T = []


def T(tid, label, cat, pats, anti=(), ambiguous=False, requires=None):
    """requires: a regex that MUST appear within REQUIRE_WINDOW chars of the match.

    Used for tokens whose bare form is a common English word or another product's
    name ("Phoenix" the city, "Lambda" the GPU cloud), where a generic stack-context
    gate is not discriminating enough.
    """
    _T.append((tid, label, cat, list(pats), list(anti), ambiguous, requires))


# ---- languages -------------------------------------------------------------
T("python", "Python", "language", [r"\bpython\b", r"\bpython[23]\b", r"\bcpython\b"])
T("javascript", "JavaScript", "language", [r"\bjavascript\b", r"\bjs\b(?!on)", r"\bes6\b", r"\becmascript\b"],
  anti=[r"\bnode\.?js\b", r"\breact\.?js\b", r"\bvue\.?js\b", r"\bnext\.?js\b", r"\bnest\.?js\b",
        r"\bthree\.?js\b", r"\bd3\.?js\b", r"\bember\.?js\b", r"\bbackbone\.?js\b", r"\bjs\w"])
T("typescript", "TypeScript", "language", [r"\btypescript\b", r"(?-i:\bTS\b)\s*/\s*(?-i:\bJS\b)", r"\bts/js\b"])
T("java", "Java", "language", [r"\bjava\b(?!\s*script)", r"\bjvm\b", r"\bj2ee\b"])
T("c", "C", "language", [r"(?-i:(?<![\w+#.])C(?![\w+#.]))", r"\bansi\s+c\b", r"\bembedded\s+c\b",
                         r"\bc99\b", r"\bc11\b", r"\bc\s+programming\b"],
  anti=[r"\bc[\s-]*level\b", r"\bc[\s-]*suite\b", r"\bseries\s+c\b", r"\bc\s*corp", r"\bvitamin\s+c\b",
        r"\bc\+\+", r"\bc#", r"\bc\.\s*[a-z]", r"\bplan\s+c\b", r"\boption\s+c\b", r"\bc\s*/\s*c\+\+",
        # the biggest source of C false positives by far: Objective-C / Obj-C / ObjC
        r"objective[\s-]?c\b", r"\bobj[\s-]?c\b", r"\bobjc\b",
        r"\bc[\s-]*corp\b", r"\bhep\s*c\b", r"\bsection\s+c\b", r"\bpart\s+c\b", r"\bexhibit\s+c\b"],
  ambiguous=True)
T("cpp", "C++", "language", [r"c\+\+", r"\bcplusplus\b"])
T("csharp", "C#", "language", [r"c#", r"\bc[\s-]?sharp\b"])
T("go", "Go", "language", [r"\bgolang\b", r"(?-i:\bGo\b)", r"(?-i:\bGO\b)"],
  anti=[r"\bgo\s+(?:to|for|from|with|beyond|deep|live|public|remote|big|fast|further|above|back|"
        r"through|over|into|out|up|down|ahead|straight|hand|the|a|an|and|or|it|we|you|they|"
        r"home|global|all[- ]in|full[- ]time|on|off|by|get|make|build|see|read|check|apply|visit|"
        r"learn|find|head|team|company|forward|far|wrong|right|well|beyond)\b",
        r"\b(?:on|to|let'?s|lets|ready|want|wants|wanted|willing|able|going|gonna|will|can|could|"
        r"should|would|must|need|needs|needed|here|there|where|when|how|why|who|that|this|"
        r"the)\s+go\b",
        r"\bgo[- ]?to\b", r"\bon[- ]the[- ]go\b", r"\bgo[- ]getter\b", r"\bgo[- ]live\b",
        r"\bgo\s*\)", r"\bgo!", r"\bgo\.\s*[A-Z]", r"\bgo\s+public\b", r"\bpokemon\s+go\b",
        r"\bgo\s+where\b", r"\bmust[- ]go\b"],
  ambiguous=True)
T("rust", "Rust", "language", [r"(?-i:\bRust\b)", r"\brustlang\b", r"\brust\s+lang\b"],
  anti=[r"\brust\s+belt\b", r"\brusty\b", r"\brust\s+(?:on|of)\s+(?:the\s+)?(?:metal|steel|iron)\b"],
  ambiguous=True)
T("ruby", "Ruby", "language", [r"\bruby\b", r"\bmri\s+ruby\b", r"\bjruby\b"],
  anti=[r"\bruby\s+(?:tuesday|receptionist|red|slipper|ring|anniversary)\b", r"\bruby\s+on\s+rails\b"],
  ambiguous=True)
T("php", "PHP", "language", [r"\bphp\b", r"\bphp[578]\b"])
T("scala", "Scala", "language", [r"\bscala\b"], anti=[r"\bla\s+scala\b", r"\bscalab", r"\bscalar\b"])
T("kotlin", "Kotlin", "language", [r"\bkotlin\b"])
T("swift", "Swift", "language", [r"(?-i:\bSwift\b)", r"\bswiftui\b"],
  anti=[r"\bswift\s+(?:payment|transfer|message|code|network|bic|wire|banking|action|response|"
        r"delivery|execution|iteration|decision|feedback|growth|progress|and\s+decisive)\b",
        r"\btaylor\s+swift\b", r"\bswift(?:ly)\b", r"\b(?:fast|quick|move)\s+and\s+swift\b",
        r"\bswift\s*/\s*sepa\b", r"\biso\s*20022\b.{0,20}swift\b"],
  ambiguous=True)
T("objective_c", "Objective-C", "language", [r"objective[\s-]?c\b", r"\bobjc\b"])
T("elixir", "Elixir", "language", [r"\belixir\b"])
T("erlang", "Erlang", "language", [r"\berlang\b", r"\botp\b(?=.{0,30}\berlang\b)"])
T("clojure", "Clojure", "language", [r"\bclojure(?:script)?\b", r"\bclj\b"])
T("haskell", "Haskell", "language", [r"\bhaskell\b", r"\bghc\b"])
T("r", "R", "language", [r"(?-i:(?<![\w.])R(?![\w.&]))", r"\brstudio\b", r"\br\s+programming\b",
                         r"\bshiny\s+app"],
  anti=[r"\br\s*&\s*d\b", r"\br\s*/\s*d\b", r"\br\.\s*[a-z]", r"\bseries\s+r\b", r"\bpart\s+r\b",
        r"\br\s*&\s*b\b", r"\bhr\b", r"\br\s*\.\s*$"],
  ambiguous=True)
T("sql", "SQL", "language", [r"\bsql\b", r"\bt-sql\b", r"\bpl/sql\b", r"\bplpgsql\b"])
T("perl", "Perl", "language", [r"\bperl\b"])
T("zig", "Zig", "language", [r"(?-i:\bZig\b)", r"\bziglang\b"], anti=[r"\bzig[\s-]?zag"], ambiguous=True)
T("julia", "Julia", "language", [r"(?-i:\bJulia\b)", r"\bjulialang\b"],
  anti=[r"\bjulia\s+[A-Z]", r"\bjulia'", r"\b(?:by|from|contact|ask|email|reach)\s+julia\b"],
  ambiguous=True)
T("lua", "Lua", "language", [r"\blua\b", r"\bluajit\b"], anti=[r"\bkuala\s+lumpur\b"])
T("dart", "Dart", "language", [r"(?-i:\bDart\b)"],
  anti=[r"\bdart\s?board\b", r"\bdarts\b", r"\bdart\s+(?:throw|around|out|in\b)"], ambiguous=True)
T("solidity", "Solidity", "language", [r"\bsolidity\b"])
T("nim", "Nim", "language", [r"(?-i:\bNim\b)", r"\bnimlang\b"], anti=[r"\bnimble\b"], ambiguous=True)
T("assembly", "Assembly", "language", [r"\bassembly\s+(?:language|code|programming)\b", r"\bx86\s+asm\b"])
T("bash", "Bash/Shell", "language", [r"\bbash\b", r"\bshell\s+script", r"\bzsh\b", r"\bposix\s+shell\b"])

# ---- frontend --------------------------------------------------------------
T("react", "React", "frontend", [r"(?-i:\bReact\b)(?!\s+(?:to|quickly|fast|swiftly))", r"\breact\.?js\b",
                                 r"\breactjs\b"],
  anti=[r"\breact\s+(?:to|quickly|fast|swiftly|well|appropriately)\b", r"\breact\s+native\b",
        r"\breacting\b", r"\breaction"])
T("vue", "Vue", "frontend", [r"\bvue\.?js\b", r"\bvuejs\b", r"(?-i:\bVue\b)", r"\bnuxt\b"])
T("angular", "Angular", "frontend", [r"\bangular(?:js)?\b"], anti=[r"\bangular\s+(?:momentum|velocity|shape)\b"])
T("svelte", "Svelte", "frontend", [r"\bsvelte(?:kit)?\b"])
T("nextjs", "Next.js", "frontend", [r"\bnext\.js\b", r"\bnextjs\b"])
T("htmx", "htmx", "frontend", [r"\bhtmx\b"])
T("tailwind", "Tailwind CSS", "frontend", [r"\btailwind(?:css)?\b"],
  anti=[r"\btailwinds?\s+(?:of|from|in\s+the\s+market|behind)\b", r"\bmarket\s+tailwind"], ambiguous=True)
T("css", "CSS/HTML", "frontend", [r"\bcss\b", r"\bhtml5?\b", r"\bscss\b", r"\bsass\b"])
T("jquery", "jQuery", "frontend", [r"\bjquery\b"])
T("webpack", "Webpack/Vite", "frontend", [r"\bwebpack\b", r"\bvite\b(?!\s*ss)", r"\besbuild\b", r"\brollup\.?js\b"])

# ---- backend frameworks ----------------------------------------------------
T("django", "Django", "framework", [r"\bdjango\b"])
T("flask", "Flask", "framework", [r"\bflask\b"], anti=[r"\bflask\s+of\b"])
T("fastapi", "FastAPI", "framework", [r"\bfastapi\b", r"\bfast\s?api\b"])
T("rails", "Ruby on Rails", "framework", [r"\bruby\s+on\s+rails\b", r"\bror\b", r"\brails\b"],
  anti=[r"\bguard\s?rails\b", r"\boff\s+the\s+rails\b", r"\bhand\s?rails\b", r"\brails?\s+against\b"],
  ambiguous=True)
T("spring", "Spring", "framework", [r"\bspring\s*boot\b", r"\bspring\s*(?:mvc|framework|cloud|data|batch|security)\b",
                                    r"(?-i:\bSpring\b)"],
  anti=[r"\bspring\s+(?:20\d\d|semester|break|summer|of\s+20|term|season|cleaning|forward)\b",
        r"\b(?:this|next|last|in|by|since|during|until)\s+spring\b", r"\bspring[\s-]?loaded\b",
        r"\bhot\s+spring", r"\bsilver\s+spring\b", r"\bspring,\s*(?:tx|texas)\b"],
  ambiguous=True)
T("express", "Express.js", "framework", [r"\bexpress\.?js\b", r"\bexpressjs\b", r"(?-i:\bExpress\b)"],
  anti=[r"\bexpress\s+(?:yourself|your|interest|our|their|his|her|the|an?|delivery|shipping|"
        r"lane|written|in\s+writing|opinions?|ideas?|enthusiasm|excitement)\b",
        r"\b(?:to|please|can|freely|clearly|will|would|we|you|they|and|or)\s+express\b",
        r"\bexpressed\b", r"\bexpression", r"\bamerican\s+express\b", r"\bexpressive\b"],
  ambiguous=True)
T("nestjs", "NestJS", "framework", [r"\bnest\.?js\b", r"\bnestjs\b"])
T("laravel", "Laravel", "framework", [r"\blaravel\b"])
T("dotnet", ".NET", "framework", [r"\.net\b", r"\bdotnet\b", r"\basp\.net\b", r"\bnet\s?core\b", r"\bblazor\b"],
  anti=[r"\b(?:www|github|gitlab|io|com|org)\.net\b"])
T("phoenix", "Phoenix (Elixir)", "framework", [r"\bphoenix\s*(?:framework|liveview)?\b"],
  anti=[r"\bphoenix,?\s*(?:az|arizona)\b", r"\bphoenix\s+(?:metro|area|office|based|hq)\b",
        r"\bthe\s+phoenix\b", r"\bphoenix\s+project\b",
        r"\b(?:in|near|from|around|to|relocate\s+to)\s+phoenix\b",
        r"\bphoenix\s*,\s*(?:los\s+angeles|boston|new\s+york|chicago|denver|dallas|austin|"
        r"seattle|atlanta|houston|nyc|sf|san\s+\w+|tucson|scottsdale|remote)\b"],
  # "Phoenix" is a US city; only count it as the framework when Elixir/BEAM is nearby
  requires=r"\belixir\b|\bliveview\b|\babsinthe\b|\becto\b|\bbeam\b|\berlang\b|\bpetal\b|"
           r"\bphoenix\s+framework\b|\bexunit\b|\bnerves\b|\boban\b",
  ambiguous=True)
T("nodejs", "Node.js", "framework", [r"\bnode\.?js\b", r"\bnodejs\b", r"\bdeno\b", r"\bbun\b(?=.{0,40}\b(?:js|runtime|node)\b)"])
T("graphql", "GraphQL", "practice", [r"\bgraphql\b", r"\bapollo\s+(?:client|server|graph)\b", r"\bhasura\b"])
T("grpc", "gRPC", "practice", [r"\bgrpc\b", r"\bprotocol\s+buffers\b", r"\bprotobuf\b"])
T("rest_api", "REST APIs", "practice", [r"\brest(?:ful)?\s+(?:api|service|web\s+service|endpoint)",
                                        r"\brestful\b", r"\bopenapi\b", r"\bswagger\b"])
T("microservices", "Microservices", "practice", [r"\bmicro[\s-]?services?\b"])

# ---- data ------------------------------------------------------------------
T("postgres", "PostgreSQL", "data", [r"\bpostgres(?:ql)?\b", r"\bpsql\b", r"\bpg\s?bouncer\b", r"\btimescale(?:db)?\b"])
T("mysql", "MySQL", "data", [r"\bmysql\b", r"\bmariadb\b", r"\bpercona\b"])
T("mongodb", "MongoDB", "data", [r"\bmongo(?:db)?\b"])
T("redis", "Redis", "data", [r"\bredis\b", r"\bvalkey\b", r"\bmemcached\b"])
T("elasticsearch", "Elasticsearch", "data", [r"\belastic\s?search\b", r"\bopensearch\b", r"\belk\s+stack\b",
                                             r"\blucene\b", r"\bsolr\b"])
T("kafka", "Kafka", "data", [r"\bkafka\b", r"\bconfluent\b", r"\bredpanda\b"])
T("snowflake", "Snowflake", "data", [r"\bsnowflake\b"], anti=[r"\bsnowflakes\b", r"\bspecial\s+snowflake\b"])
T("databricks", "Databricks", "data", [r"\bdatabricks\b", r"\bdelta\s+lake\b"])
T("spark", "Apache Spark", "data", [r"\bapache\s+spark\b", r"\bpyspark\b", r"\bspark\s+(?:streaming|sql|jobs?|cluster)\b",
                                    r"(?-i:\bSpark\b)"],
  anti=[r"\bspark(?:s|ed|ing|le|ling)\b", r"\bspark\s+(?:joy|of|your|our|the\s+imagination|capital|"
        r"notion|interest|conversation|debate|change|innovation|curiosity)\b",
        r"\bsparkpost\b", r"\bsparkfun\b", r"\bspark\s+plug\b", r"\bignite\s+the\s+spark\b",
        r"\bspark\s*java\b", r"\bsparkjava\b"],
  ambiguous=True)
T("dbt", "dbt", "data", [r"\bdbt\b"], anti=[r"\bdbt\s+therapy\b", r"\bdialectical\b"], ambiguous=True)
T("clickhouse", "ClickHouse", "data", [r"\bclick\s?house\b"])
T("duckdb", "DuckDB", "data", [r"\bduck\s?db\b"])
T("airflow", "Airflow", "data", [r"\bairflow\b", r"\bdagster\b", r"\bprefect\b(?=.{0,40}\b(?:pipeline|orchestrat|data)\b)",
                                 r"\bluigi\b"])
T("bigquery", "BigQuery", "data", [r"\bbig\s?query\b"])
T("cassandra", "Cassandra/DynamoDB", "data", [r"\bcassandra\b", r"\bscylla(?:db)?\b", r"\bdynamo\s?db\b"])
T("hadoop", "Hadoop/Hive", "data", [r"\bhadoop\b", r"\bhive\b(?=.{0,60}\b(?:hadoop|data|query|warehouse|spark)\b)",
                                    r"\bmapreduce\b", r"\bhdfs\b"])
T("etl", "ETL/Data Warehouse", "data", [r"\betl\b", r"\belt\s+pipeline", r"\bdata\s+warehous", r"\bdata\s+lake\b",
                                        r"\bdata\s+pipelines?\b", r"\bredshift\b"])

# ---- infrastructure --------------------------------------------------------
T("aws", "AWS", "infra", [r"\baws\b", r"\bamazon\s+web\s+services\b", r"\bec2\b", r"\bs3\b", r"\brds\b"])
T("gcp", "GCP", "infra", [r"\bgcp\b", r"\bgoogle\s+cloud\b", r"\bgke\b"])
T("azure", "Azure", "infra", [r"\bazure\b"])
T("kubernetes", "Kubernetes", "infra", [r"\bkubernetes\b", r"\bk8s\b", r"\bk3s\b", r"\bhelm\s+chart", r"\beks\b", r"\baks\b"])
T("docker", "Docker", "infra", [r"\bdocker\b", r"\bcontaineri[sz]", r"\bpodman\b", r"\bdocker[\s-]?compose\b"])
T("terraform", "Terraform", "infra", [r"\bterraform\b", r"\bopentofu\b", r"\bpulumi\b"])
T("ansible", "Ansible", "infra", [r"\bansible\b", r"\bpuppet\b", r"\bchef\b(?=.{0,40}\b(?:puppet|ansible|config|infra|devops)\b)",
                                  r"\bsaltstack\b"])
T("nomad", "Nomad (HashiCorp)", "infra", [r"(?-i:\bNomad\b)", r"\bhashicorp\s+nomad\b"],
  anti=[r"\bdigital\s+nomads?\b", r"\bnomads?\b\s*(?:friendly|life|lifestyle|visa)", r"\bnomadic\b",
        r"\bnomads\b", r"\bnomad\s+(?:list|capital|health|lane)\b"],
  ambiguous=True)
T("serverless", "Serverless", "infra", [r"\bserverless\b", r"\bcloudflare\s+workers\b", r"\bvercel\b", r"\bfly\.io\b"])
T("lambda", "AWS Lambda", "infra", [r"\baws\s+lambda\b", r"\blambda\s+functions?\b", r"(?-i:\bLambda\b)"],
  anti=[r"\blambda\s+school\b", r"\blambda\s+(?:calculus|expressions?|labs?|syntax|the\s+ultimate)\b",
        r"\blambda\s+is\s+a\b", r"\blambda\s+(?:gpu|cloud|cluster)\b",
        r"\b(?:java|scala|python|kotlin|c\+\+|c#|ruby|js|javascript)\s*\d*\s+lambdas?\b",
        r"\bhalf[\s-]life\b"],
  # only count Lambda when the surrounding text is unmistakably AWS/serverless
  requires=r"\baws\b|\bamazon\b|\bserverless\b|\bdynamo\s?db\b|\bapi\s*gateway\b|\bcloudformation\b|"
           r"\bkinesis\b|\bstep\s+functions?\b|\bsqs\b|\bsns\b|\bcloudwatch\b|\bfargate\b|\bcdk\b|"
           r"\bec2\b|\bs3\b|\bglue\b|\bathena\b|\becs\b|\beks\b|\bcognito\b|\bappsync\b|\brds\b|"
           r"\bterraform\b|\bsam\b|\bedge\s+function",
  ambiguous=True)
T("linux", "Linux", "infra", [r"\blinux\b", r"\bubuntu\b", r"\bdebian\b", r"\bnixos\b", r"\bunix\b"])
T("cicd", "CI/CD", "practice", [r"\bci\s*/\s*cd\b", r"\bcicd\b", r"\bcontinuous\s+(?:integration|delivery|deployment)\b",
                                r"\bgithub\s+actions\b", r"\bjenkins\b", r"\bgitlab\s+ci\b", r"\bcircleci\b"])
T("observability", "Observability", "practice", [r"\bobservability\b", r"\bprometheus\b", r"\bgrafana\b",
                                                 r"\bdatadog\b", r"\bopentelemetry\b", r"\bsentry\b", r"\bsplunk\b"])

# ---- ai / ml ---------------------------------------------------------------
T("machine_learning", "Machine Learning", "ai", [r"\bmachine\s+learning\b", r"\bml\b(?!\s*/?\s*(?:s\b))",
                                                 r"\bml\s+(?:engineer|model|pipeline|ops|infra|platform)\b"],
  anti=[r"\b\d+\s*ml\b", r"\bhtml\b", r"\bxml\b", r"\byaml\b", r"\btoml\b", r"\bml\s+of\b"])
T("deep_learning", "Deep Learning", "ai", [r"\bdeep\s+learning\b", r"\bneural\s+net", r"\bdnn\b", r"\bcnn\b(?!\s*news)"])
T("tensorflow", "TensorFlow", "ai", [r"\btensor\s?flow\b", r"\bkeras\b", r"\bjax\b(?=.{0,60}\b(?:ml|model|train|neural|numpy|research)\b)"])
T("pytorch", "PyTorch", "ai", [r"\bpy\s?torch\b", r"\btorch\b(?=.{0,40}\b(?:model|train|neural|gpu|cuda)\b)"])
T("llm", "LLMs", "ai", [r"\bllms?\b", r"\blarge\s+language\s+models?\b", r"\bfoundation\s+models?\b",
                        r"\bfine[\s-]?tun\w*\s+(?:llm|model|gpt)"])
T("gpt", "GPT / OpenAI", "ai", [r"\bgpt[\s-]?\d?\b", r"\bchatgpt\b", r"\bopenai\b", r"\bclaude\b(?=.{0,60}\b(?:api|anthropic|llm|model|ai)\b)",
                                r"\banthropic\b"])
T("rag", "RAG", "ai", [r"\bretrieval[\s-]augmented\s+generation\b", r"(?-i:\bRAG\b)"],
  anti=[r"\brags?\s+(?:to|and)\b", r"\bragged\b", r"\brag\s?time\b", r"\bragu\b"], ambiguous=True)
T("langchain", "LangChain", "ai", [r"\blang\s?chain\b", r"\bllama\s?index\b", r"\bllamaindex\b", r"\bhaystack\b"])
T("transformers", "Transformers", "ai", [r"\bhugging\s?face\b", r"\btransformers?\b(?=.{0,80}\b(?:model|nlp|bert|llm|"
                                         r"pytorch|tensorflow|hugging|attention|architecture)\b)",
                                         r"\bbert\b", r"\battention\s+is\s+all\b"],
  anti=[r"\btransformers?\s+(?:movie|toy|robot)\b", r"\bpower\s+transformer"])
T("nlp", "NLP", "ai", [r"\bnlp\b", r"\bnatural\s+language\s+processing\b", r"\bspacy\b", r"\bnltk\b"])
T("computer_vision", "Computer Vision", "ai", [r"\bcomputer\s+vision\b", r"\bopencv\b", r"\bimage\s+recognition\b",
                                               r"\bobject\s+detection\b", r"\bocr\b"])
T("cuda", "CUDA / GPU", "ai", [r"\bcuda\b", r"\bgpu\s+(?:kernel|programming|cluster|compute|optimi)", r"\btriton\b",
                               r"\bnvidia\b"])
T("mlops", "MLOps", "ai", [r"\bml\s?ops\b", r"\bmlflow\b", r"\bkubeflow\b", r"\bweights\s*&\s*biases\b",
                           r"\bfeature\s+store\b", r"\bmodel\s+serving\b"])
T("vector_db", "Vector databases", "ai", [r"\bvector\s+(?:database|db|store|search|index)", r"\bpinecone\b",
                                          r"\bweaviate\b", r"\bqdrant\b", r"\bmilvus\b", r"\bpgvector\b",
                                          r"\bchroma\s?db\b", r"\bfaiss\b", r"\bembeddings?\b"])
T("ai_agents", "AI agents", "ai", [r"\bai\s+agents?\b", r"\bagentic\b", r"\bmulti[\s-]agent\b",
                                   r"\bmcp\b(?=.{0,60}\b(?:server|protocol|context|tool|claude|llm)\b)",
                                   r"\bcursor\b(?=.{0,60}\b(?:ai|code|copilot|claude|agent)\b)",
                                   r"\bcopilot\b", r"\bclaude\s+code\b"])
T("data_science", "Data science stack", "ai", [r"\bpandas\b", r"\bnumpy\b", r"\bscikit[\s-]?learn\b", r"\bsklearn\b",
                                               r"\bjupyter\b", r"\bmatplotlib\b", r"\bstatistic(?:s|al)\s+model"])

# ---- platforms / other -----------------------------------------------------
T("ios", "iOS", "platform", [r"(?-i:\biOS\b)", r"(?-i:\bIOS\b)", r"\bxcode\b", r"\bapple\s+platform"],
  anti=[r"\bcisco\s+ios\b"])
T("android", "Android", "platform", [r"\bandroid\b"])
T("react_native", "React Native", "platform", [r"\breact\s?native\b"])
T("flutter", "Flutter", "platform", [r"\bflutter\b"])
T("unity", "Unity / Unreal", "platform", [r"\bunity\s?3?d?\b(?=.{0,80}\b(?:game|engine|vr|ar|3d|unreal|c#)\b)",
                                          r"\bunreal\s+engine\b", r"\bunity\s+engine\b", r"\bgodot\b"])
T("wasm", "WebAssembly", "platform", [r"\bweb\s?assembly\b", r"\bwasm\b", r"\bwasi\b"])
T("embedded", "Embedded / firmware", "platform", [r"\bembedded\b", r"\bfirmware\b", r"\brtos\b", r"\bmicrocontroller\b",
                                                  r"\bfpga\b", r"\bverilog\b", r"\bvhdl\b", r"\bbare[\s-]metal\b",
                                                  r"\bstm32\b", r"\barduino\b", r"\braspberry\s?pi\b"])
T("blockchain", "Blockchain", "other", [r"\bblockchain\b", r"\bethereum\b", r"\bbitcoin\b", r"\bsmart\s+contracts?\b",
                                        r"\bdefi\b", r"\bzk[\s-]?(?:snark|rollup|proof)"])
T("web3", "Web3 / crypto", "other", [r"\bweb\s?3\b", r"\bcrypto(?:currency)?\b", r"\bnft\b", r"\bdao\b(?=.{0,40}\b(?:crypto|web3|token|governance)\b)"])
T("security", "Security / crypto-eng", "other", [r"\bcyber\s?security\b", r"\binfosec\b", r"\bappsec\b",
                                                 r"\bpen(?:etration)?\s+test", r"\bcryptograph", r"\bsoc\s?2\b",
                                                 r"\bzero\s+trust\b", r"\bthreat\s+model"])
T("elastic_cloud_other", "Cloudflare/CDN/edge", "infra", [r"\bcloudflare\b", r"\bfastly\b", r"\bcdn\b", r"\bedge\s+comput"])

TECHS = {}
for tid, label, cat, pats, anti, amb, req in _T:
    TECHS[tid] = {"label": label, "category": cat, "patterns": pats,
                  "anti_patterns": anti, "ambiguous": amb, "requires": req}

# words that, near an ambiguous token, indicate a technology-stack context
QUALIFIER_RE = re.compile(
    r"\b(?:programming|languages?|tech\s*stack|technolog\w*|stack|codebase|"
    r"written\s+in|built\s+(?:with|in|on)|we\s+use|using|use\s+of|experience\s+(?:with|in)|"
    r"proficien\w*|expertise|familiar\s+with|our\s+(?:stack|tools|toolchain)|micro\s?services?|"
    r"framework|libraries|library|backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack|"
    r"knowledge\s+of|skills?\s*:|stack\s*:|tech\s*:|tools?\s*:|bonus|nice\s+to\s+have|"
    r"plus\s+points|must\s+have|working\s+(?:with|knowledge))\b", re.I)

def _alt(patterns):
    """One alternation per technology -- 120 scans per posting instead of ~330."""
    return re.compile("|".join("(?:%s)" % p for p in patterns), re.I)


_COMPILED = {}
for tid, spec in TECHS.items():
    _COMPILED[tid] = {
        "pat": _alt(spec["patterns"]),
        "anti": _alt(spec["anti_patterns"]) if spec["anti_patterns"] else None,
        "req": re.compile(spec["requires"], re.I) if spec["requires"] else None,
        "ambiguous": spec["ambiguous"],
    }
UNAMBIGUOUS_IDS = [t for t in TECHS if not TECHS[t]["ambiguous"]]
AMBIGUOUS_IDS = [t for t in TECHS if TECHS[t]["ambiguous"]]
_UNAMB_ITEMS = [(t, _COMPILED[t]["pat"], _COMPILED[t]["anti"]) for t in UNAMBIGUOUS_IDS]
_AMB_ITEMS = [(t, _COMPILED[t]["pat"], _COMPILED[t]["anti"]) for t in AMBIGUOUS_IDS]


def match_techs(text, return_detail=False):
    """Multi-label technology extraction with anti-patterns + context gating.

    An unambiguous technology is accepted on any match that is not overlapped by one of
    its own anti-patterns.  An ambiguous one additionally needs an unambiguous technology
    or a stack-qualifier word within CONTEXT_WINDOW characters of the surviving match.
    """
    accepted, detail = set(), []

    # pass 1: unambiguous technologies (cheap boolean scan, no spans needed yet)
    for tid, pat, anti in _UNAMB_ITEMS:
        m = pat.search(text)
        if m is None:
            continue
        if anti is not None:
            spans = [(a.start(), a.end()) for a in anti.finditer(text)]
            if spans:
                ok = False
                for h in pat.finditer(text):
                    if not any(a_s < h.end() and h.start() < a_e for a_s, a_e in spans):
                        ok = True
                        break
                if not ok:
                    continue
        accepted.add(tid)

    # pass 2: ambiguous technologies
    amb_hits = {}
    for tid, pat, _anti in _AMB_ITEMS:
        hits = [(m.start(), m.end()) for m in pat.finditer(text)]
        if hits:
            amb_hits[tid] = hits
    if not amb_hits:
        return (sorted(accepted), detail) if return_detail else sorted(accepted)

    anchors = []
    for tid in accepted:
        anchors.extend((m.start(), m.end()) for m in _COMPILED[tid]["pat"].finditer(text))
    qual = [(m.start(), m.end()) for m in QUALIFIER_RE.finditer(text)]

    for tid, hits in amb_hits.items():
        anti = _COMPILED[tid]["anti"]
        req = _COMPILED[tid]["req"]
        anti_spans = [(m.start(), m.end()) for m in anti.finditer(text)] if anti else []
        for (s, e) in hits:
            if any(a_s < e and s < a_e for a_s, a_e in anti_spans):
                continue                                   # killed by an anti-pattern
            if req is not None and not req.search(text[max(0, s - REQUIRE_WINDOW):e + REQUIRE_WINDOW]):
                continue                                   # missing the mandatory context
            lo, hi = s - CONTEXT_WINDOW, e + CONTEXT_WINDOW
            if any(a_s < hi and lo < a_e for a_s, a_e in anchors) or \
               any(q_s < hi and lo < q_e for q_s, q_e in qual):
                accepted.add(tid)
                if return_detail:
                    detail.append((tid, text[max(0, s - 70):e + 70].replace("\n", " ")))
                break
    return (sorted(accepted), detail) if return_detail else sorted(accepted)


# ---------------------------------------------------------------------------
# 3. role taxonomy
# ---------------------------------------------------------------------------

ROLES = {
    "backend": {"label": "Backend", "patterns": [
        r"\bback[\s-]?end\b", r"\bserver[\s-]?side\b", r"\bapi\s+engineer\b",
        r"\bdistributed\s+systems?\s+engineer\b", r"\bsystems?\s+engineer\b",
        r"\bplatform\s+engineer\b(?=.{0,40}\bapi\b)"]},
    "frontend": {"label": "Frontend", "patterns": [
        r"\bfront[\s-]?end\b", r"\bui\s+engineer\b", r"\bweb\s+developer\b",
        r"\bjavascript\s+(?:engineer|developer)\b", r"\breact\s+(?:engineer|developer)\b"]},
    "fullstack": {"label": "Full-stack", "patterns": [
        r"\bfull[\s-]?stack\b", r"\bgeneralist\s+engineer\b", r"\bproduct\s+engineer\b"]},
    "mobile": {"label": "Mobile", "patterns": [
        r"\bmobile\s+(?:engineer|developer|dev)\b", r"\bios\s+(?:engineer|developer|dev)\b",
        r"\bandroid\s+(?:engineer|developer|dev)\b", r"\breact\s?native\s+(?:engineer|developer)\b",
        r"\bflutter\s+(?:engineer|developer)\b"]},
    "data-engineering": {"label": "Data engineering", "patterns": [
        r"\bdata\s+engineer", r"\banalytics\s+engineer", r"\bdata\s+platform\b",
        r"\betl\s+(?:engineer|developer)\b", r"\bdata\s+infrastructure\b", r"\bbig\s+data\s+engineer"]},
    "data-science": {"label": "Data science / analytics", "patterns": [
        r"\bdata\s+scien", r"\bdata\s+analyst\b", r"\bbusiness\s+intelligence\b", r"\bbi\s+analyst\b",
        r"\bquantitative\s+(?:analyst|research)", r"\bquant\s+(?:developer|researcher|trader)\b",
        r"\bstatistician\b"]},
    "ml-ai": {"label": "ML / AI", "patterns": [
        r"\bml\s+engineer", r"\bmachine\s+learning\s+(?:engineer|scientist|researcher)",
        r"\bai\s+engineer", r"\bai\s+researcher\b", r"\bapplied\s+scientist\b", r"\bdeep\s+learning\s+engineer",
        r"\bnlp\s+engineer", r"\bcomputer\s+vision\s+engineer", r"\bresearch\s+engineer\b",
        r"\bai\s+(?:scientist|developer)\b", r"\bllm\s+engineer\b", r"\bmlops\s+engineer\b"]},
    "devops-sre-platform": {"label": "DevOps / SRE / Platform", "patterns": [
        r"\bdev\s?ops\b", r"\bsre\b", r"\bsite\s+reliability\b", r"\bplatform\s+engineer",
        r"\binfrastructure\s+engineer", r"\bcloud\s+(?:engineer|architect)\b",
        r"\bsystems?\s+administrator\b", r"\bsysadmin\b", r"\bkubernetes\s+engineer\b",
        r"\bproduction\s+engineer\b", r"\bbuild\s+engineer\b", r"\brelease\s+engineer\b"]},
    "security": {"label": "Security", "patterns": [
        r"\bsecurity\s+(?:engineer|analyst|architect|researcher|specialist)\b", r"\bappsec\b",
        r"\binfosec\b", r"\bciso\b", r"\bpenetration\s+tester\b", r"\bcryptograph\w*\s+engineer\b",
        r"\bsecurity\s+operations\b"]},
    "qa-test": {"label": "QA / Test", "patterns": [
        r"\bqa\s+(?:engineer|analyst|lead|automation)\b", r"\bquality\s+assurance\b",
        r"\btest\s+(?:engineer|automation)\b", r"\bsdet\b", r"\bautomation\s+(?:engineer|tester)\b"]},
    "embedded-hardware": {"label": "Embedded / hardware", "patterns": [
        r"\bembedded\s+(?:engineer|software|systems|developer)\b", r"\bfirmware\s+engineer\b",
        r"\bhardware\s+engineer\b", r"\belectrical\s+engineer\b", r"\brobotics\s+engineer\b",
        r"\bfpga\s+engineer\b", r"\basic\s+(?:engineer|design)\b", r"\bmechanical\s+engineer\b"]},
    "design-ux": {"label": "Design / UX", "patterns": [
        r"\b(?:ux|ui)\s*/?\s*(?:ui|ux)?\s*designer\b", r"\bproduct\s+designer\b", r"\bux\s+researcher\b",
        r"\bgraphic\s+designer\b", r"\bdesign\s+(?:lead|manager|director)\b", r"\bvisual\s+designer\b",
        r"\bbrand\s+designer\b", r"\bdesigner\b"]},
    "product-management": {"label": "Product management", "patterns": [
        r"\bproduct\s+manager\b", r"\bproduct\s+management\b", r"\btechnical\s+product\s+manager\b",
        r"\bproduct\s+owner\b", r"\bhead\s+of\s+product\b", r"\bvp\s+(?:of\s+)?product\b",
        r"\bprogram\s+manager\b", r"\bproject\s+manager\b"]},
    "engineering-management": {"label": "Engineering management", "patterns": [
        r"\bengineering\s+manager\b", r"\bem\b(?=.{0,20}\bengineering\b)", r"\bhead\s+of\s+engineering\b",
        r"\bvp\s+(?:of\s+)?engineering\b", r"\bdirector\s+of\s+engineering\b", r"\bcto\b",
        r"\btech(?:nical)?\s+lead\b", r"\bteam\s+lead(?:er)?\b", r"\bengineering\s+director\b",
        r"\bchief\s+technology\s+officer\b"]},
    "developer-relations": {"label": "Developer relations", "patterns": [
        r"\bdev(?:eloper)?\s+(?:relations|advocate|evangelist|experience)\b", r"\bdevrel\b",
        r"\bcommunity\s+manager\b", r"\btechnical\s+writer\b", r"\bdocumentation\s+engineer\b"]},
    "support": {"label": "Support / SE / Ops", "patterns": [
        r"\b(?:technical|customer)\s+support\b", r"\bsupport\s+engineer\b", r"\bsolutions?\s+(?:engineer|architect)\b",
        r"\bcustomer\s+success\b", r"\bimplementation\s+(?:engineer|consultant)\b",
        r"\bforward[\s-]deployed\b", r"\boperations\s+(?:manager|associate|analyst)\b"]},
    "sales-marketing": {"label": "Sales / Marketing / GTM", "patterns": [
        r"\b(?:account\s+executive|sales\s+(?:engineer|rep|manager|lead|director)|sdr\b|bdr\b)",
        r"\bmarketing\s+(?:manager|lead|director|associate|specialist)\b", r"\bgrowth\s+(?:lead|manager|marketer)\b",
        r"\bhead\s+of\s+(?:sales|marketing|growth|revenue)\b", r"\bdemand\s+gen", r"\bcontent\s+marketer\b",
        r"\bvp\s+(?:of\s+)?(?:sales|marketing)\b"]},
    "research": {"label": "Research", "patterns": [
        r"\bresearch\s+scientist\b", r"\bresearcher\b", r"\bpost[\s-]?doc\b", r"\bphd\s+(?:position|candidate)\b",
        r"\bscientist\b(?!.{0,20}\bdata\b)"]},
}
_ROLE_C = {rid: _alt(spec["patterns"]) for rid, spec in ROLES.items()}
_ROLE_ITEMS = sorted(_ROLE_C.items())
# generic engineering fallback when nothing more specific fires
GENERIC_ENG_RE = re.compile(
    r"\b(?:software\s+engineer\w*|software\s+developer|swe|engineer(?:ing|s)?|developer|"
    r"programmer|hacker|coder|technical\s+staff)\b", re.I)


def match_roles(header, full):
    """Header first (that is the role field); fall back to the whole posting."""
    for zone, src in ((header, "header"), (full, "body")):
        found = [rid for rid, pat in _ROLE_ITEMS if pat.search(zone)]
        if found:
            return found, src
    if GENERIC_ENG_RE.search(header) or GENERIC_ENG_RE.search(full):
        return ["other-engineering"], "generic"
    return ["other"], "none"


# ---------------------------------------------------------------------------
# 4. remote classification
# ---------------------------------------------------------------------------

NEG_REMOTE = re.compile(
    r"\bno\s+remote\b|\bnot\s+remote\b|\bnon[\s-]remote\b|\bno\s+remote\s+work\b|"
    r"\bremote\s*[:=]\s*no\b|\bnot\s+a\s+remote\b|\bno\s+telecommut\w*|\bno\s+wfh\b|"
    r"\bon[\s-]?site\s+only\b|\bonsite\s+only\b|\bin[\s-]?office\s+only\b|\boffice\s+only\b|"
    r"\blocal\s+(?:candidates?|only|applicants?)\b|\bmust\s+be\s+(?:on[\s-]?site|in[\s-]?office)\b|"
    r"\bno\s+remote\s+(?:candidates?|positions?|applicants?|option)\b|\bsorry,?\s+no\s+remote\b|"
    r"\bremote\s+is\s+not\b|\bthis\s+is\s+not\s+a\s+remote\b|\bwe\s+do\s+not\s+(?:offer|do)\s+remote\b|"
    r"\bnot\s+open\s+to\s+remote\b|\bno\s+fully\s+remote\b", re.I)

ONSITE_POS = re.compile(
    r"\bon[\s-]?site\b|\bonsite\b|\bin[\s-]?office\b|\bin\s+the\s+office\b|\boffice[\s-]based\b|"
    r"\bin[\s-]?person\b|\brelocation\s+required\b|\bmust\s+relocate\b|\bco[\s-]?located\b|"
    r"\bwork\s+from\s+(?:the\s+)?office\b|\bfrom\s+our\s+office\b|\boffice\s+attendance\b", re.I)

# A specific office CITY / metro.  Deliberately kept disjoint from REGION_RE below:
# "Remote (US)" or "Remote (Europe)" is fully remote with a scope, whereas
# "Remote or SF" / "Austin, TX or Remote" names an office and is therefore hybrid.
CITY_RE = re.compile(
    r"\b(?:san\s+francisco|sf\s+bay\s+area|\bsf\b|south\s+bay|silicon\s+valley|palo\s+alto|"
    r"mountain\s+view|menlo\s+park|sunnyvale|santa\s+clara|san\s+jose|oakland|berkeley|"
    r"new\s+york(?:\s+city)?|\bnyc\b|brooklyn|manhattan|jersey\s+city|"
    r"boston|cambridge,?\s*ma|somerville|"
    r"seattle|bellevue|redmond|portland|austin|dallas|houston|san\s+antonio|denver|boulder|"
    r"chicago|atlanta|miami|orlando|tampa|philadelphia|pittsburgh|baltimore|"
    r"washington,?\s*d\.?c\.?|arlington|detroit|ann\s+arbor|minneapolis|madison|"
    r"salt\s+lake\s+city|phoenix,?\s*az|scottsdale|las\s+vegas|san\s+diego|los\s+angeles|"
    r"\bla\b(?=\s*[,|)/])|santa\s+monica|pasadena|irvine|nashville|charlotte|raleigh|durham|"
    r"columbus|cleveland|cincinnati|kansas\s+city|st\.?\s+louis|indianapolis|milwaukee|"
    r"providence|hartford|new\s+haven|princeton|"
    r"toronto|vancouver|montreal|ottawa|waterloo|calgary|"
    r"london|manchester|edinburgh|glasgow|bristol|cambridge,?\s*uk|oxford|"
    r"berlin|munich|m[uü]nchen|hamburg|frankfurt|cologne|k[oö]ln|stuttgart|d[uü]sseldorf|"
    r"paris|lyon|toulouse|amsterdam|rotterdam|utrecht|eindhoven|brussels|antwerp|"
    r"z[uü]rich|geneva|basel|vienna|wien|prague|praha|warsaw|krak[oó]w|wroc[lł]aw|"
    r"budapest|bucharest|sofia|belgrade|zagreb|ljubljana|"
    r"stockholm|gothenburg|copenhagen|oslo|helsinki|reykjavik|"
    r"dublin|cork|lisbon|porto|madrid|barcelona|valencia|milan|rome|turin|athens|"
    r"tel\s+aviv|jerusalem|haifa|istanbul|dubai|abu\s+dhabi|riyadh|cairo|nairobi|lagos|"
    r"cape\s+town|johannesburg|"
    r"bangalore|bengaluru|mumbai|pune|hyderabad|chennai|gurgaon|gurugram|noida|delhi|"
    r"singapore|hong\s+kong|shanghai|beijing|shenzhen|tokyo|osaka|kyoto|seoul|taipei|"
    r"bangkok|manila|jakarta|kuala\s+lumpur|ho\s+chi\s+minh|hanoi|"
    r"sydney|melbourne|brisbane|perth|auckland|wellington|"
    r"s[aã]o\s+paulo|rio\s+de\s+janeiro|buenos\s+aires|santiago|bogot[aá]|"
    r"mexico\s+city|guadalajara|montevideo|lima|"
    r"moscow|st\.?\s+petersburg|kyiv|kiev|lviv|minsk|tbilisi|yerevan|tallinn|riga|vilnius)\b"
    r"|,\s*(?:CA|NY|TX|WA|MA|IL|CO|OR|GA|FL|PA|NC|VA|MD|DC|NJ|AZ|UT|MN|OH|MI|TN|WI|MO|IN|NV|"
    r"CT|SC|AL|KY|LA|OK|IA|AR|KS|NM|NE|ID|WV|HI|ME|NH|RI|MT|DE|SD|ND|AK|VT|WY)\b", re.I)

# A company office mentioned by name ("LA office", "Boston HQ") -- an office anchor, not a scope.
OFFICE_RE = re.compile(r"(?<!no )\boffices?\b|\bHQ\b|\bheadquarter", re.I)

# Country / continent / multi-country scope: these qualify a remote role, they are not an office.
REGION_RE = re.compile(
    r"\b(?:usa?|u\.s\.a?\.?|united\s+states|canada|mexico|brazil|argentina|chile|colombia|"
    r"uk|united\s+kingdom|england|scotland|ireland|france|germany|deutschland|spain|portugal|"
    r"italy|netherlands|holland|belgium|switzerland|austria|poland|czechia|czech\s+republic|"
    r"hungary|romania|bulgaria|greece|serbia|croatia|slovenia|slovakia|ukraine|"
    r"sweden|norway|denmark|finland|iceland|estonia|latvia|lithuania|"
    r"israel|turkey|india|china|japan|korea|taiwan|vietnam|thailand|indonesia|philippines|"
    r"malaysia|australia|new\s+zealand|south\s+africa|kenya|nigeria|egypt|"
    r"europe|european\s+union|\beu\b|\beea\b|emea|apac|latam|latin\s+america|"
    r"north\s+america|south\s+america|asia|africa|americas|worldwide|global|anywhere)\b", re.I)

HYBRID = re.compile(
    r"\bhybrid\b|\bremote[\s-]?friendly\b|\bpartially\s+remote\b|\bpartial(?:ly)?[\s-]remote\b|"
    r"\bpart[\s-]remote\b|\bpart[\s-]time\s+remote\b|\bsome\s+remote\b|\bremote\s+possible\b|"
    r"\bremote\s+optional\b|\bremote\s+ok(?:ay)?\b|\bremote\s+considered\b|\bremote\s+available\b|"
    r"\bopen\s+to\s+remote\b|\bmostly\s+remote\b|\boccasional(?:ly)?\s+remote\b|\bflexible\s+remote\b|"
    r"\b\d\s*(?:\+)?\s*days?\s+(?:a|per)?\s*week?\s*(?:in|at)\s+(?:the\s+)?office\b|"
    r"\b\d\s*days?\s+in[\s-]?(?:the\s+)?office\b|\b\d\s*days?\s+on[\s-]?site\b|"
    r"\b\d\s*(?:[-\u2013]\s*\d\s*)?(?:x|times|days?)\s*(?:/|per|a)\s*(?:week|month)"
    r"[^|\n]{0,25}\b(?:in|at)\s+(?:the\s+)?(?:office|hq)\b|"
    r"\b(?:in|at)\s+(?:the\s+)?office\s+\d\s*(?:[-\u2013]\s*\d\s*)?(?:x|times|days?)\b|"
    r"\bremote\s*/\s*(?:on[\s-]?site|office|hybrid)\b|\b(?:on[\s-]?site|office)\s*/\s*remote\b|"
    r"\bremote\s+or\s+(?:on[\s-]?site|in[\s-]?office|office)\b|"
    r"\b(?:on[\s-]?site|in[\s-]?office)\s+or\s+remote\b|\bremote\s+w\s*/\s*travel\b|"
    r"\bwfh\s+\d\s*days?\b|\bhybrid[\s-]remote\b|\bflex(?:ible)?\s+(?:work|office|hybrid)\b", re.I)

REMOTE_POS = re.compile(
    r"\bremote\b|\bwfh\b|\bwork\s+from\s+home\b|\bwork\s+from\s+anywhere\b|\bdistributed\s+team\b|"
    r"\bfully[\s-]distributed\b|\btelecommut\w*|\banywhere\s+in\s+the\s+world\b", re.I)

REMOTE_STRONG = re.compile(
    r"\b100%\s*remote\b|\bfully\s+remote\b|\bremote[\s-]first\b|\bremote[\s-]only\b|"
    r"\ball[\s-]remote\b|\bremote\s*\(|\bREMOTE\b|\bremote\s+position\b|\bremote\s+role\b|"
    r"\bwork\s+from\s+anywhere\b|\bfully\s+distributed\b|\bremote\s*[:=]\s*yes\b|"
    r"\bremote\s+worldwide\b|\bremote\s+global\b", re.I)


def classify_remote(loc_zone, header_zone, body):
    """Tiered: header location fields -> first 3 lines -> whole posting.

    Precedence inside a tier:
      1. explicit negation ("no remote", "onsite only")            -> onsite
      2. explicit hybrid token ("hybrid", "2 days in office", ...)  -> hybrid
      3. remote token + an explicit office CITY in the same zone    -> hybrid
         ("Austin, TX or Remote", "Remote or SF")
      4. remote token (optionally with a country/region scope)      -> remote
      5. onsite token, or a bare office city with no remote token   -> onsite

    Returns (class, tier_that_decided).  loc_zone is the header WITHOUT the company
    field so that names like "Boston Dynamics" cannot masquerade as an office.
    """
    tiers = ((loc_zone, "header"), (header_zone, "header_zone"), (body, "body"))
    for zone, tier in tiers:
        if not zone:
            continue
        if NEG_REMOTE.search(zone):
            return "onsite", tier
        if HYBRID.search(zone):
            return "hybrid", tier
        rem = REMOTE_POS.search(zone)
        ons = ONSITE_POS.search(zone)
        if rem:
            if tier != "body" and (ons or CITY_RE.search(zone) or OFFICE_RE.search(zone)):
                return "hybrid", tier
            return "remote", tier
        if ons:
            return "onsite", tier
        if tier != "body" and (CITY_RE.search(zone) or OFFICE_RE.search(zone)):
            return "onsite", tier          # a named office with no remote token
        if tier != "body" and REGION_RE.search(zone):
            return "onsite", tier          # bare country/region, no remote token
    return "unknown", "none"


LOCATIONISH = re.compile(
    r"\b(?:san\s+francisco|sf\b|new\s+york|nyc\b|london|berlin|paris|amsterdam|toronto|austin|"
    r"seattle|boston|chicago|denver|los\s+angeles|la\b|palo\s+alto|mountain\s+view|cambridge|"
    r"dublin|barcelona|madrid|lisbon|zurich|munich|vienna|stockholm|copenhagen|oslo|helsinki|"
    r"warsaw|prague|budapest|bucharest|kyiv|kiev|moscow|tel\s+aviv|bangalore|bengaluru|mumbai|"
    r"delhi|singapore|tokyo|sydney|melbourne|auckland|vancouver|montreal|sao\s+paulo|"
    r"buenos\s+aires|mexico\s+city|dubai|nairobi|lagos|cape\s+town|hong\s+kong|shanghai|beijing|"
    r"seoul|taipei|bangkok|manila|jakarta|remote|onsite|on[\s-]site|hybrid|anywhere|worldwide|"
    r"usa?\b|u\.s\.|united\s+states|uk\b|united\s+kingdom|canada|germany|france|spain|italy|"
    r"netherlands|poland|portugal|sweden|norway|denmark|finland|switzerland|austria|belgium|"
    r"ireland|israel|india|australia|brazil|argentina|mexico|japan|china|korea|europe|eu\b|emea|"
    r"apac|latam|africa|asia|"
    r"[A-Z]{2},\s*(?:USA?|United\s+States)|,\s*(?:CA|NY|TX|WA|MA|IL|CO|OR|GA|FL|PA|NC|VA|MD|DC|"
    r"NJ|AZ|UT|MN|OH|MI|TN|WI|MO|IN|NV|CT|SC|AL|KY|LA|OK|IA|AR|KS|NM|NE|ID|WV|HI|ME|NH|RI|MT|"
    r"DE|SD|ND|AK|VT|WY)\b)", re.I)

# Precedence: the first rule that fires wins.  Multi-country rules are checked before
# single-country ones so "Remote (US/Canada)" lands on north-america rather than us.
# Two-letter acronyms are wrapped in (?-i:...) so only the uppercase form matches --
# lowercase "us" is the English pronoun and lowercase "eu"/"it" are noise.
SCOPE_RULES = [
    ("global", r"\bworld[\s-]?wide\b|\bglobal(?:ly)?\b|\banywhere\b|\bany\s+(?:time\s*zone|timezone)\b|"
               r"\bany\s+country\b|\bfrom\s+anywhere\b|\bearth\b|\bthe\s+planet\b"),
    ("north-america", r"\bnorth\s+america\b|\bnorth[\s-]american\b|(?-i:\bUS\b)\s*[/&+]\s*\bcanada\b|"
                      r"(?-i:\bUS\b)\s+(?:and|or)\s+canada\b|\bcanada\s*[/&+]\s*(?-i:\bUS\b)"),
    ("us", r"(?-i:\bUS\b)|(?-i:\bU\.S\.)|(?-i:\bUSA\b)|\bunited\s+states\b|\bus[\s-]based\b|"
           r"\bus\s+only\b|\bcontinental\s+us\b|\bconus\b|\bus\s+citizens?\b"),
    ("canada", r"\bcanada\b|\bcanadian\b"),
    ("eu", r"(?-i:\bEU\b)|\beurope(?:an)?\b|(?-i:\bEEA\b)|\beurozone\b|\bcentral\s+europe\b|"
           r"\bwestern\s+europe\b"),
    ("uk", r"(?-i:\bUK\b)|\bunited\s+kingdom\b|\bengland\b|\bbritain\b"),
    ("latam", r"\blatam\b|\blatin\s+america\b|\bsouth\s+america\b|\bbrazil\b|\bargentina\b|\bmexico\b"),
    ("apac", r"(?-i:\bAPAC\b)|\basia[\s-]pacific\b|\bsoutheast\s+asia\b|\baustralia\b|"
             r"\bnew\s+zealand\b|\bjapan\b|\bsingapore\b"),
    ("india", r"\bindia\b|(?-i:\bIST\b)"),
    ("emea", r"(?-i:\bEMEA\b)|\bmiddle\s+east\b|\bafrica\b"),
    ("same-timezone", r"\btime\s*zones?\b|\btimezones?\b|\boverlap\b|"
                      r"(?-i:\bCET\b|\bCEST\b|\bEST\b|\bEDT\b|\bPST\b|\bPDT\b|\bGMT\b|\bUTC\b|\bBST\b)|"
                      r"\+/-\s*\d|\+\s*\d\s*h"),
]
_SCOPE_C = [(k, re.compile(p, re.I)) for k, p in SCOPE_RULES]


def remote_scope(zone):
    """Single scope label with a documented precedence (first rule that fires wins)."""
    if not zone:
        return None
    for key, rx in _SCOPE_C:
        if rx.search(zone):
            return key
    return None


# ---------------------------------------------------------------------------
# 5. header / company / title / location / seniority / salary / visa
# ---------------------------------------------------------------------------

SENIORITY_RULES = [
    ("exec", r"\b(?:cto|ceo|coo|cpo|ciso|cfo|chief\s+\w+\s+officer|vp\s+of\s+|vp,|vice\s+president|"
             r"head\s+of\s+|founding\s+(?:engineer|team)|co[\s-]?founder|director\s+of\s+)"),
    ("principal", r"\b(?:principal|distinguished|fellow|architect)\b"),
    ("staff", r"\b(?:staff|senior\s+staff|tech(?:nical)?\s+lead|lead\s+(?:engineer|developer|"
              r"scientist|designer)|team\s+lead)\b"),
    ("manager", r"\b(?:engineering\s+manager|manager|management|director)\b"),
    ("senior", r"\b(?:senior|sr\.?|snr\.?|experienced)\b"),
    ("junior", r"\b(?:junior|jr\.?|entry[\s-]level|graduate|new\s+grad|early\s+career|associate)\b"),
    ("intern", r"\b(?:intern(?:ship)?s?|co[\s-]?op\s+student|apprentice(?:ship)?|trainee)\b"),
    ("mid", r"\b(?:mid[\s-]?level|intermediate|mid\b)"),
]
_SEN_C = [(k, re.compile(p, re.I)) for k, p in SENIORITY_RULES]


def seniority_of(header, full):
    for zone in (header, full[:1500]):
        for key, rx in _SEN_C:
            if rx.search(zone):
                return key
    return "unknown"


MONEY = re.compile(
    r"(?P<cur>[$€£₹]|\bUSD\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b|\bCHF\b|\bINR\b|\bSEK\b|\bPLN\b)?\s*"
    r"(?P<a>\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s*[kK]\b|\d{5,7})"
    r"\s*(?:-|–|—|to|~|\.\.)\s*"
    r"(?P<cur2>[$€£₹]|\bUSD\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b|\bCHF\b|\bINR\b)?\s*"
    r"(?P<b>\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s*[kK]\b|\d{5,7})")
SINGLE_MONEY = re.compile(
    r"(?P<cur>[$€£₹])\s*(?P<a>\d{2,3}(?:\.\d)?\s*[kK]\b|\d{1,3}(?:,\d{3})+|\d{5,7})\s*(?P<plus>\+)?")
SALARY_NOISE = re.compile(
    r"\b(?:raised|funding|round|series\s+[a-e]|valuation|arr\b|revenue|budget|customers?|users?|"
    r"equity|market\s+cap|invest\w*|grant|prize|savings?)\b", re.I)


def _num(tok):
    tok = tok.strip().replace(",", "")
    if tok.lower().endswith("k"):
        return int(float(tok[:-1].strip()) * 1000)
    return int(float(tok))


def parse_salary(header, full):
    """Best-effort USD range. Returns (min, max, currency) with None where unknown."""
    for zone in (header, full[:2500]):
        if not zone:
            continue
        for m in MONEY.finditer(zone):
            ctx = zone[max(0, m.start() - 60):m.end() + 40]
            if SALARY_NOISE.search(ctx):
                continue
            try:
                lo, hi = _num(m.group("a")), _num(m.group("b"))
            except ValueError:
                continue
            if not (10_000 <= lo <= 1_500_000 and lo < hi <= 2_000_000):
                continue
            cur = (m.group("cur") or m.group("cur2") or "").strip().upper()
            cur = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "": None}.get(cur, cur)
            return lo, hi, cur
        for m in SINGLE_MONEY.finditer(zone):
            ctx = zone[max(0, m.start() - 60):m.end() + 40]
            if SALARY_NOISE.search(ctx):
                continue
            try:
                v = _num(m.group("a"))
            except ValueError:
                continue
            if not (10_000 <= v <= 1_500_000):
                continue
            cur = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}[m.group("cur")]
            return v, None, cur
    return None, None, None


# The HN header convention writes sponsorship as a bare field: "... | ONSITE | VISA | ...".
VISA_FIELD = re.compile(r"(?:^|\||,|;)\s*(?:visa|h[\s-]?1[\s-]?b)(?:\s+(?:ok|welcome|transfer[s]?|"
                        r"sponsorship|sponsored))?\s*(?:\||,|;|$)", re.I)
VISA_YES = re.compile(r"\bvisa\s+(?:sponsor|support|assistance|available|offered|ok|yes|welcome)|"
                      r"\bwe\s+sponsor\b|\bsponsorship\s+(?:available|offered|provided|possible)|"
                      r"\bwill\s+sponsor\b|\bh1b\s+(?:transfer|sponsor|welcome|ok)|"
                      r"\bvisa\s*[:=]\s*yes\b|\bcan\s+sponsor\b|\bhappy\s+to\s+sponsor\b|"
                      r"\bsponsor\s+visas?\b|\brelocation\s+and\s+visa\b", re.I)
# checked BEFORE VISA_YES: "no visa sponsorship" contains "visa sponsor"
VISA_NO = re.compile(r"\bno\s+visa\b|\bcannot\s+sponsor\b|\bcan'?t\s+sponsor\b|\bunable\s+to\s+sponsor\b|"
                     r"\bno\s+sponsorship\b|\bnot\s+(?:able\s+to\s+|currently\s+|)sponsor|"
                     r"\bvisa\s*[:=]\s*no\b|\bwe\s+do\s+not\s+sponsor\b|\bdon'?t\s+sponsor\b|"
                     r"\bno\s+h1b\b|\bno\s+visa\s+sponsorship\b|\bwithout\s+sponsorship\b|"
                     r"\bnot\s+offer(?:ing)?\s+(?:visa\s+)?sponsorship\b|\bno\s+relocation\s+or\s+visa\b",
                     re.I)
RELOC_YES = re.compile(r"\brelocation\s+(?:assistance|package|support|offered|available|paid|bonus)|"
                       r"\bwe\s+(?:offer|provide|pay\s+for)\s+relocation\b|\brelocation\s*[:=]\s*yes\b|"
                       r"\bwill\s+relocate\s+you\b|\|\s*relo(?:cation)?\s*\|", re.I)
# checked BEFORE RELOC_YES: "no relocation assistance" contains "relocation assistance"
RELOC_NO = re.compile(r"\bno\s+relocation\b|\brelocation\s*[:=]\s*no\b|\bno\s+relo\b|"
                      r"\bwe\s+do\s+not\s+(?:offer|provide)\s+relocation\b|"
                      r"\bnot\s+offer(?:ing)?\s+relocation\b|\bwithout\s+relocation\b", re.I)
RELO_FIELD = re.compile(r"(?:^|\||,|;)\s*relo(?:cation)?\s*(?:\||,|;|$)", re.I)


def visa_flag(header, full):
    """True / False / None. Negations are checked before positives, and the bare 'VISA'
    header field (the HN convention for 'we sponsor') is checked before either."""
    if VISA_FIELD.search(header):
        return True
    if VISA_NO.search(full):
        return False
    if VISA_YES.search(full):
        return True
    return None


def relocation_flag(header, full):
    if RELO_FIELD.search(header):
        return True
    if RELOC_NO.search(full):
        return False
    if RELOC_YES.search(full):
        return True
    return None

COMPANY_STRIP = re.compile(r"\s*\((?:yc\s*[wsfa]?\d*|ycombinator)[^)]*\)\s*", re.I)
COMPANY_TAIL = re.compile(r"\s*(?:\bis\s+hiring\b|\bhiring\b|\bwe(?:'re| are)\s+hiring\b|"
                          r"\bjobs?\b|\bcareers?\b|[-–—:]).*$", re.I)


def split_header(text):
    """Return (header_line, header_zone, fields, loc_zone).

    loc_zone is the header with the leading company field removed -- that is the part
    of the header where the location / work-mode field lives, and dropping the company
    keeps names like "Boston Dynamics" or "Phoenix Labs" from reading as an office.
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return "", "", [], ""
    header = lines[0]
    # some posts put the company alone on line 1 and the pipe header on line 2
    if header.count("|") < 2 and len(lines) > 1 and lines[1].count("|") >= 2:
        header = header + " | " + lines[1]
    header = header[:400]
    zone = "\n".join(lines[:3])[:600]
    if "|" in header:
        fields = [f.strip() for f in header.split("|") if f.strip()]
    else:
        fields = [header.strip()]
    loc_zone = " | ".join(fields[1:]) if len(fields) > 1 else header
    return header, zone, fields, loc_zone


ROLE_WORD = re.compile(r"\b(?:engineer|developer|designer|scientist|manager|analyst|architect|"
                       r"lead|director|intern|devops|sre|qa|pm\b|marketer|writer|researcher|"
                       r"consultant|specialist|administrator|programmer|head\s+of|vp\b|cto\b|"
                       r"recruiter|technician|dev\b|swe\b)", re.I)


def _is_place(s):
    """True when the string is (essentially) just a place name.

    Pre-2016 posts often open with the location ("Toronto - Senior Java Developer",
    "SF | Back-End Engineer"), and taking that as the company name would be wrong.
    "Boston Dynamics" is NOT a place: the city match must cover almost the whole string.
    """
    s = s.strip()
    for rx in (CITY_RE, REGION_RE):
        m = rx.match(s)
        if m and m.end() >= len(s) - 2:
            return True
    return bool(re.fullmatch(r"(?:remote|onsite|on-site|hybrid|anywhere|worldwide)", s, re.I))


def extract_fields(header, fields):
    company = title = location = None
    if fields:
        c = COMPANY_STRIP.sub(" ", fields[0])
        c = COMPANY_TAIL.sub("", c).strip(" .,-–—|")
        c = URL_RE.sub("", c).strip(" .,-–—|")
        if 1 <= len(c) <= 60 and not _is_place(c):
            company = c
    if len(fields) > 1:
        # title = first field after the company that reads like a role
        for f in fields[1:]:
            if ROLE_WORD.search(f) and len(f) <= 120:
                title = f
                break
        if title is None:
            title = fields[1][:120]
        # location = highest-scoring location-ish field (prefer later fields, skip the title)
        best, best_score = None, 0
        for i, f in enumerate(fields[1:], 1):
            if f is title:
                continue
            hits = len(LOCATIONISH.findall(f))
            if hits and len(f) <= 120:
                score = hits + (1 if i >= 2 else 0)
                if score > best_score:
                    best, best_score = f, score
        location = best
    if location is None and LOCATIONISH.search(header):
        location = header[:160]
    return company, title, location


# ---------------------------------------------------------------------------
# 6. is this comment actually a job posting?
# ---------------------------------------------------------------------------

POSTINGISH_LEGACY = re.compile(
    r"\b(?:hiring|we(?:'re| are)\s+looking|join\s+(?:us|our)|apply|role|position|engineer|"
    r"developer|designer|salary|full[\s-]?time|part[\s-]?time|contract|remote|onsite|"
    r"opening|opportunit|team|candidates?|experience)\b", re.I)
# ^ kept ONLY so --filter-audit can reproduce the old behaviour for comparison.  Two bugs:
#   (a) the trailing \b kills every plural -- "Senior Engineers wanted", "Multiple openings"
#       and "Software engineers- Java/C#" all failed to match, and the "opportunit"
#       alternative could never match at all because a letter always follows it;
#   (b) it only looked at text[:600], so any ad that opens with a company blurb was lost.
# It dropped 2408 comments; hand-reading 40 of them showed ~87% were ordinary job ads, and
# the loss was onsite-skewed and concentrated in 2011-2015 (5.2-13.5% of those years).

# The replacement is deliberately HIGH-RECALL.  ~97.5% of top-level comments in these
# threads really are job ads, so a strict filter costs far more real postings than it
# removes noise; the residual non-postings it lets through are reported in meta.
HIRING_RE = re.compile(
    r"\b(?:hiring|hires?|hired|looking\s+for|seeking|we\s+need|wanted|join\s+(?:us|our|the\s+team)|"
    r"appl(?:y|ying|ication\w*|icants?)|roles?|positions?|openings?|opportunit\w*|vacanc\w*|jobs?|"
    r"career\w*|engineer\w*|developers?|programmers?|designers?|scientists?|analysts?|architects?|"
    r"sysadmins?|devops|sre|interns?|internships?|salar\w+|compensation|equity|benefits|"
    r"full[\s-]?time|part[\s-]?time|contract\w*|freelanc\w*|remote|onsite|on-site|hybrid|"
    r"relocation|visas?|sponsorship|teams?|candidates?|applicants?|resumes?|r\u00e9sum\u00e9s?|cv|"
    r"recruit\w*|employ\w*|experience|stack)\b", re.I)

# "Company: X / Location: Y / Role: Z" -- the other common ad layout, which can contain
# none of the words above.  Two or more labelled fields is the accept threshold.
LABELLED_FIELD_RE = re.compile(
    r"(?mi)^\s*(?:company|location|locations|role|roles|position|positions|job\s*title|title|"
    r"salary|compensation|tech(?:nolog\w*)?|stack|contact|e-?mail|url|website|type|remote|"
    r"start\s*date|apply|about)\s*[:\-\u2013]")

# an applicant-facing link is itself evidence of an ad ("http://squareup.com/jobs")
JOBS_URL_RE = re.compile(
    r"https?://[^\s]*(?:jobs?|careers?|hiring|greenhouse\.io|lever\.co|workable|smartrecruiters|"
    r"jobvite|bamboohr|recruitee|ashbyhq|breezy\.hr|applytojob|workday|taleo|angel\.co)", re.I)

MIN_POSTING_CHARS = 60


def is_posting(header, text):
    """True when a top-level comment looks like a job ad.  Tuned for RECALL -- see above.

    Accepts on any one of four independent signals, all read over the WHOLE comment:
      1. the de-facto pipe header (>=2 pipes), decisive at any length;
      2. >=2 labelled fields ("Company:" / "Location:" / ...);
      3. any hiring-vocabulary token;
      4. an applicant-facing jobs/careers/ATS URL.
    Signals 3 and 4 additionally require the comment to reach MIN_POSTING_CHARS, which is
    what keeps one-line chatter ("Duolingo", "Who is NOT hiring?") out.
    """
    if header.count("|") >= 2:
        return True
    if len(LABELLED_FIELD_RE.findall(text)) >= 2:
        return True
    if len(text) < MIN_POSTING_CHARS:
        return False
    return bool(HIRING_RE.search(text) or JOBS_URL_RE.search(text))


# ---------------------------------------------------------------------------
# 7. hand-labelled remote-classifier test set (real strings from the corpus)
# ---------------------------------------------------------------------------
# (header string, expected class).  Collected by sampling real header lines across
# 2011-2026 and labelling them by hand; see --audit for the sampling helper.

# --- set A: CURATED.  35 real header strings picked to cover the hard patterns
# (explicit negation, "X or Remote", N-days-in-office, remote+HQ, bare city, bare country).
# Chosen after reading the corpus, so this set is NOT unbiased -- it measures whether the
# rules the classifier is *supposed* to implement actually fire.  Set B below is the
# honest generalisation estimate.
REMOTE_TESTS_CURATED = [
    ("Web application developer - REMOTE", "remote"),
    ("Rails Machine | Site Reliability Engineer | Full-Time | Remote | Hiring Junior to Senior Levels", "remote"),
    ("Foreground | Sr. Software Engineer | REMOTE US | Full-time | https://www.foreground.co", "remote"),
    ("TurboTenant | Senior Software Engineer | Full-time | REMOTE - Contract", "remote"),
    ("Sleeper | Senior Software Engineers | Remote (US only) | Full-Time | https://sleeper.com/jobs", "remote"),
    ("Abnormal Security I Engineering I Remote (USA, Canada, UK) I Full-Time I https://abnormalsecurity.com/", "remote"),
    ("Coefficient (https://coefficient.io/about/) | Multiple Roles | Fully Remote | Full time | VC-Backed startup", "remote"),
    ("Contra | Senior Site Reliability Engineer | $145-165k + equity & benefits | Full-time | Fully Remote", "remote"),
    ("Metabase | https://metabase.com/ | Remote (Global) | Full-time | Applied AI Engineers, Engineering Managers", "remote"),
    ("Better Stack | https://betterstack.com | Full-stack Engineer | Europe remote in UTC +/- 3h", "remote"),
    ("Puma.tech | Remote-first with PST overlap | Engineering & Growth | $75-150k base & 200k+ equity", "remote"),
    ("Optic Power - San Juan, PR | REMOTE or Outside Mainland USA | JavaScript Developers", "remote"),
    ("SF | Back-End Engineer | Lead DevOps Engineer | ONSITE", "onsite"),
    ("Tock | Chicago, IL | Engineering and Design", "onsite"),
    ("Streamable - Brooklyn, New York - Full Time - Onsite", "onsite"),
    ("StreetEasy, a Zillow Group (Nasdaq: Z) brand | New York, NY | FULL-TIME | ONSITE", "onsite"),
    ("Rock Pamper Scissors | Nottingham, UK | Full time", "onsite"),
    ("Shopify | Senior Data Engineer | Waterloo, Ontario, Canada | On-site | Full-time", "onsite"),
    ("DRW | London, UK | Onsite | Full-Time | drw.com", "onsite"),
    ("Lightmeter | YC W22 | Berlin", "onsite"),
    ("Coram AI | Backend Engineer | Full-Time | London, UK | ONSITE", "onsite"),
    ("HockeyStack | Backend Engineer | San Francisco, ONSITE, VISA", "onsite"),
    ("Langfuse (https://langfuse.com) | Backend Engineer, Product Engineer | Berlin Germany | in-person | Full-time", "onsite"),
    ("NerdWallet - San Francisco, CA - relocation and H1B ok (sorry, no remote)", "onsite"),
    ("London, UK - Pusher - Full time - No Remote", "onsite"),
    ("Prezi (https://prezi.com) - Budapest, Hungary - Full time (no remote, yes relocation)", "onsite"),
    ("DMSi | Senior JavaScript Engineer | Omaha, NE | Onsite Only | Full Time | $100k+", "onsite"),
    ("TurboML (https://turboml.com/) | India | Enterprise Sales Manager/ Software Engineer / ML Engineer", "onsite"),
    ("Cooklist | Django / React Native / CTO | San Francisco, CA / Dallas, TX / Remote | $90K - 120K", "hybrid"),
    ("Login.gov | REMOTE or Washington, DC | Software Engineers, Site Reliability Engineers | Full-Time", "hybrid"),
    ("Highstreet Mobile | Backend Engineer | Utrecht (near Amsterdam), The Netherlands | Onsite/Remote (EU) | Full Time", "hybrid"),
    ("FUTO | https://futo.org | Austin, TX or Remote | Full time and interns", "hybrid"),
    ("Cambly.com | San Francisco, Ca, USA | Hybrid/Onsite | Full Time", "hybrid"),
    ("Absinthe Labs | Senior Full-Stack Engineer | Remote or NYC Hybrid", "hybrid"),
    ("Onos Health | Lead Engineer | Full-Time | San Francisco | 1-2x/week in office | $200-240k + Equity", "hybrid"),
]

# --- set B: HELD-OUT.  42 header strings drawn at random (seed 20260902, one comment per
# thread, shuffled) and labelled BLIND -- the labels below were written down before the
# classifier was ever run on them.  This is the honest accuracy estimate.
REMOTE_TESTS_HELDOUT = [
    ("Dave.com is hiring senior engineers in LA.", "onsite"),
    ("Elektron | Rust Graphics Engineer | Full-time | Onsite | Gothenburg, Sweden | https://www.elektron.se", "onsite"),
    ("VideoLabs | Software Developer | Paris, France | REMOTE, INTERNS", "hybrid"),
    ("Flutter (YC W12) - Gesture Recognition thru Webcame (Flutter.io)", "unknown"),
    ("evervault (https://evervault.com/) | Product Engineer | Dublin, Ireland | ONSITE | 60k - 80k + meaningful equity stake", "onsite"),
    ("Rails Hacker/ Chief Technical Officer", "unknown"),
    ("Boston, MA", "onsite"),
    ("We are hiring aggressively at FaunaDB for Scala and JVM distributed systems roles, as well as field engineering/solution architect roles.", "unknown"),
    ("Paperless Post (https://www.paperlesspost.com/) | New York, NY | Software Engineers | Full-time | Visa | Onsite", "onsite"),
    ("financial.com FDC India | Kochi (Kerala, India) | ONSITE (up to 50% remote) | Full-time", "hybrid"),
    ("Toronto - PagerDuty - Senior Software Engineer", "onsite"),
    ("Hall, now in SOMA is hiring. Learn more about our move: https://hall.com/blog/hall-headquarters-move-to-san-francisc...", "onsite"),
    ("Location: Palo Alto, CA/Glendale, CA", "onsite"),
    ("PawBoost | Engineering Team Lead | Raleigh, NC | REMOTE or ONSITE | Full Time", "hybrid"),
    ("SerpApi | https://serpapi.com | Senior Web Engineer | Based in Austin, TX but everyone is remote | Full-time | ONSITE or FULLY REMOTE (We're a remote first company) | $150k 1099", "hybrid"),
    ("New York / London - Bloomberg", "onsite"),
    ("MoneyDolly | Generalist Data Engineer | REMOTE - US | https://moneydolly.com", "remote"),
    ("San Francisco - Software Engineer (H1B welcome)", "onsite"),
    ("Senior PHP Developer [1] - London/Reading, UK - Full time - Direct hire / no contractors", "onsite"),
    ("Orbit (stealth) | San Francisco, CA | Frontend Engineer | Full-Time | Onsite preferred", "onsite"),
    ("FreeAgent", "unknown"),
    ("LOCATION: NYC OR REMOTE", "hybrid"),
    ("Mytraffic | Python django, Typescript react, pyspark, scala spark | Remote (EU timezone) | Full-time | https://www.mytraffic.io/en/", "remote"),
    ("Rescale | San Francisco | SF & REMOTE | https://jobs.lever.co/rescale Rescale offers a software platform and hardware infrastructure", "hybrid"),
    ("Headspace Health | B2B team (multiple roles: 2 mid/ 1 senior SWE) | REMOTE(US) + SF/Santa Monica | Fulltime", "hybrid"),
    ("Slab | Engineering | Remote (Worldwide) | Full-time", "remote"),
    ("Appear (https://appear.space/) | Founding Engineer (Unity) | Oslo or REMOTE (CET +/- 2 hours) | Full Time", "hybrid"),
    ("Location: India", "onsite"),
    ("Boston, MA & Providence, RI -- Full time & interns", "onsite"),
    ("Mixpanel | Full-stack / Front-End Software Engineers | SF / ATX / SEA | Fulltime | Onsite | www.mixpanel.com", "onsite"),
    ("Sonian (Newton, MA) - Full time, local or remote", "hybrid"),
    ("REDWOOD CITY, CA - FULL TIME - ON SITE - SR. SOFTWARE ENGINEER", "onsite"),
    ("Homebound | Senior Full Stack Engineer | Full-time | Denver / Remote (US) https://jobs.lever.co/homebound/c645e77e", "hybrid"),
    ("Supabase (YC S20) || Remote || Full-time || supabase.com", "remote"),
    ("AppFolio, Inc | Application Security Engineer, Security Operations Engineer | Santa Barbara, CA (On-site, full time)", "onsite"),
    ("Moven | Support Engineer (remote but US) & Full-stack Engineer (remote worldwide) | Full-time | https://github.com/moven/jobs", "remote"),
    ("Ekotrope | Boston, MA | Full-Time | ONSITE | https://ekotrope.com", "onsite"),
    ("Hightower, New York, NY", "onsite"),
    ("Webaverse | Full-stack Software Engineer | Remote | https://github.com/webaverse", "remote"),
    ("| CONTA AS (www.conta.no) | NORWAY + Remote friendly | Frontend Developer with ELM skills |", "hybrid"),
    ("Roadie | Multiple Positions | Full-time, Fully Remote | Atlanta Based | https://www.roadie.com", "remote"),
    ("Couchbase", "unknown"),
]

RC_LABELS = ["remote", "hybrid", "onsite", "unknown"]


def run_remote_tests(tests):
    """Confusion matrix of the classifier against a hand-labelled set."""
    conf = defaultdict(int)
    wrong = []
    for s, want in tests:
        _header, zone, _fields, loc = split_header(s)
        got, _tier = classify_remote(strip_urls(loc), strip_urls(zone), strip_urls(s))
        conf[(want, got)] += 1
        if got != want:
            wrong.append((s, want, got))
    n = len(tests)
    correct = sum(v for (a, b), v in conf.items() if a == b)
    return conf, wrong, correct, n


def print_confusion(name, tests):
    conf, wrong, correct, n = run_remote_tests(tests)
    print("\n%s  (%d hand-labelled real header strings)" % (name, n))
    print("  %-11s" % "true\\pred" + "".join("%9s" % lb for lb in RC_LABELS))
    for a in RC_LABELS:
        print("  %-11s" % a + "".join("%9d" % conf.get((a, b), 0) for b in RC_LABELS))
    print("  accuracy: %d/%d = %.1f%%" % (correct, n, 100.0 * correct / n))
    if wrong:
        print("  misclassified:")
        for s, want, got in wrong:
            print("    want=%-7s got=%-7s  %s" % (want, got, s[:110]))
    return {"n": n, "correct": correct, "accuracy": round(correct / n, 4),
            "confusion": {"%s->%s" % k: v for k, v in sorted(conf.items())},
            "misclassified": [{"text": s, "want": w, "got": g} for s, w, g in wrong]}


# ---------------------------------------------------------------------------
# 8. main
# ---------------------------------------------------------------------------

def parse_thread_worker(task):
    """Parse one cached thread file. Top-level so multiprocessing can pickle it.

    task = (thread_id, month, want_audit, audit_n, header_n)
    Returns a dict of postings + counters + (already thinned) audit/header samples.
    """
    tid_thread, month, want_audit, audit_n, header_n = task
    path = HN_RAW / ("%s.json" % tid_thread)
    counters = Counter()
    postings = []
    audit = {}            # tech_id -> [seen_count, [context strings]]
    headers = []
    n_header_seen = 0
    rng = random.Random(hash(("hn", tid_thread)) & 0xFFFFFFFF)

    if not path.exists():
        counters["thread_file_missing"] += 1
        return {"postings": postings, "counters": dict(counters), "months": {},
                "audit": {}, "headers": headers}

    with open(path, encoding="utf-8") as f:
        d = json.load(f)

    n_posts = 0
    year = month[:4]
    for ch in (d.get("children") or []):
        counters["comments_in"] += 1
        counters["cy_" + year] += 1
        text = clean_html(ch.get("text"))
        if not text:
            counters["deleted_or_empty"] += 1
            continue
        header, zone, fields, loc_zone = split_header(text)
        if not is_posting(header, text):
            counters["non_posting"] += 1
            counters["dy_" + year] += 1
            continue

        matchtext = strip_urls(text)
        m_header = strip_urls(header)
        m_zone = strip_urls(zone)
        m_loc = strip_urls(loc_zone)

        rclass, tier = classify_remote(m_loc, m_zone, matchtext)
        counters["tier_" + tier] += 1
        company, title, location = extract_fields(header, fields)
        scope = None
        if rclass in ("remote", "hybrid"):
            scope = remote_scope(location or m_loc) or remote_scope(m_zone)

        if want_audit:
            techs, detail = match_techs(matchtext, return_detail=True)
            for t_id, ctx in detail:
                slot = audit.setdefault(t_id, [0, []])
                slot[0] += 1
                if len(slot[1]) < audit_n:
                    slot[1].append((month, ctx))
                else:
                    j = rng.randrange(slot[0])
                    if j < audit_n:
                        slot[1][j] = (month, ctx)
        else:
            techs = match_techs(matchtext)

        roles, role_src = match_roles(m_header, matchtext)
        counters["rolesrc_" + role_src] += 1
        sen = seniority_of(m_header, matchtext)
        smin, smax, cur = parse_salary(m_header, matchtext)

        visa = visa_flag(m_header, matchtext)
        reloc = relocation_flag(m_header, matchtext)

        if header_n:
            n_header_seen += 1
            row = (month, rclass, tier, header[:170])
            if len(headers) < 4:
                headers.append(row)
            else:
                j = rng.randrange(n_header_seen)
                if j < 4:
                    headers[j] = row

        o = {"id": ch.get("id"), "m": month, "a": ch.get("author"), "rc": rclass}
        if company:
            o["co"] = company
        if title:
            o["t"] = title
        if location:
            o["loc"] = location[:120]
        if scope:
            o["rs"] = scope
        if sen != "unknown":
            o["sr"] = sen
        if roles and roles != ["other"]:
            o["r"] = roles
        if techs:
            o["tc"] = techs
        if smin is not None:
            o["smin"] = smin
        if smax is not None:
            o["smax"] = smax
        if cur:
            o["cur"] = cur
        if visa is not None:
            o["v"] = visa
        if reloc is not None:
            o["rel"] = reloc
        o["x"] = text[:EXCERPT_CHARS]
        postings.append(o)
        counters["postings"] += 1
        n_posts += 1

    return {"postings": postings, "counters": dict(counters),
            "months": {month: n_posts}, "audit": audit, "headers": headers}


def filter_audit(n):
    """Re-derive the is_posting() error rates that justify the current filter.

    Reads every cached thread, applies both the current filter and the legacy one, and
    prints per-year drop rates for each plus N random comments from the two sets a
    reviewer needs to read: still-dropped, and newly-kept (dropped by the old filter).
    """
    def legacy(header, text):
        if len(text) < 60:
            return False
        if header.count("|") >= 2:
            return True
        return bool(POSTINGISH_LEGACY.search(text[:600]))

    manifest = read_json(THREADS)
    per_year = defaultdict(lambda: [0, 0, 0])     # year -> [comments, drop_new, drop_old]
    still, newly = [], []
    for th in manifest["threads"]:
        path = HN_RAW / ("%s.json" % th["id"])
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        y = th["month"][:4]
        for ch in (d.get("children") or []):
            text = clean_html(ch.get("text"))
            if not text:
                continue
            header = split_header(text)[0]
            new_ok, old_ok = is_posting(header, text), legacy(header, text)
            row = per_year[y]
            row[0] += 1
            row[1] += 0 if new_ok else 1
            row[2] += 0 if old_ok else 1
            if not new_ok and len(text) >= 60:
                still.append((th["month"], text))
            elif new_ok and not old_ok:
                newly.append((th["month"], text))

    print("\nIS_POSTING FILTER AUDIT -- per-year drop rate, current vs legacy")
    print("  %-6s %9s %9s %8s %9s %8s" % ("year", "comments", "drop_now", "rate", "drop_old", "rate"))
    tc = tn = to = 0
    for y in sorted(per_year):
        c, dn, do = per_year[y]
        tc, tn, to = tc + c, tn + dn, to + do
        print("  %-6s %9d %9d %7.1f%% %9d %7.1f%%"
              % (y, c, dn, 100.0 * dn / c, do, 100.0 * do / c))
    print("  %-6s %9d %9d %7.1f%% %9d %7.1f%%"
          % ("ALL", tc, tn, 100.0 * tn / tc, to, 100.0 * to / tc))
    print("  recovered by the fix: %d comments" % len(newly))

    rng = random.Random(20260902)
    for label, pool in (("STILL DROPPED (>=60 chars)", still), ("NEWLY KEPT", newly)):
        rng.shuffle(pool)
        print("\n--- %d random: %s ---" % (min(n, len(pool)), label))
        for mo, text in pool[:n]:
            print("  [%s] %s" % (mo, " / ".join(text.split("\n"))[:200]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true",
                    help="print sampled matches for every ambiguous technology token")
    ap.add_argument("--audit-n", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=0,
                    help="worker processes (0 = cpu_count-1, capped at 8; 1 = serial)")
    ap.add_argument("--sample-headers", type=int, default=0,
                    help="print N random real header lines (used to build the hand-label set)")
    ap.add_argument("--filter-audit", type=int, default=0, metavar="N",
                    help="audit is_posting(): per-year drop rates for the current and the old "
                         "(POSTINGISH_LEGACY) filter, plus N random still-dropped and N random "
                         "newly-kept comments to hand-read. Runs instead of the normal build.")
    args = ap.parse_args()

    if args.filter_audit:
        filter_audit(args.filter_audit)
        return

    # corpus text is full of non-cp1252 characters; never let a print kill a 2-minute run
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - older interpreters / redirected streams
        pass

    manifest = read_json(THREADS)
    threads = manifest["threads"]
    print("threads in manifest: %d (%s .. %s)" % (len(threads), threads[0]["month"], threads[-1]["month"]))

    # ---- classifier self-test -------------------------------------------------
    print("\n" + "=" * 72)
    print("REMOTE CLASSIFIER EVALUATION (every string is verbatim from the corpus)")
    acc_curated = print_confusion("SET A - curated hard cases", REMOTE_TESTS_CURATED)
    acc_heldout = print_confusion("SET B - random sample, labelled BLIND (headline number)",
                                  REMOTE_TESTS_HELDOUT)
    print("=" * 72)

    # ---- parse ----------------------------------------------------------------
    # One task per thread file; threads are independent so this parallelises cleanly.
    tasks = [(t["id"], t["month"], args.audit, args.audit_n, args.sample_headers)
             for t in threads]
    jobs = args.jobs if args.jobs > 0 else max(1, (os.cpu_count() or 2) - 1)
    jobs = min(jobs, 8, len(tasks))
    print("\nparsing %d threads with %d worker process(es)" % (len(tasks), jobs))

    counters = Counter()
    audit_samples = defaultdict(list)
    audit_seen = Counter()      # true count of accepted matches, corpus-wide
    audit_pool = Counter()      # count of candidates that reached the merge reservoir
    header_samples = []
    header_seen = 0
    months_seen = Counter()
    postings_out = []
    rng = random.Random(20260902)

    def absorb(res):
        """Merge one thread's result, keeping the reservoir samples corpus-wide."""
        nonlocal header_seen
        postings_out.extend(res["postings"])
        counters.update(res["counters"])
        months_seen.update(res["months"])
        for tid, (seen, samples) in res["audit"].items():
            audit_seen[tid] += seen            # true number of accepted matches
            for ctx in samples:                # workers keep <=audit_n each; pool them
                audit_pool[tid] += 1
                k = audit_pool[tid]
                if k <= args.audit_n:
                    audit_samples[tid].append(ctx)
                else:
                    j = rng.randrange(k)
                    if j < args.audit_n:
                        audit_samples[tid][j] = ctx
        for row in res["headers"]:
            header_seen += 1
            if header_seen <= args.sample_headers:
                header_samples.append(row)
            else:
                j = rng.randrange(header_seen)
                if j < args.sample_headers:
                    header_samples[j] = row

    t_start = time.time()
    if jobs == 1:
        for i, task in enumerate(tasks, 1):
            absorb(parse_thread_worker(task))
            if i % 20 == 0 or i == len(tasks):
                print("  [%3d/%d] %s  %d postings so far  (%.0fs)"
                      % (i, len(tasks), task[1], len(postings_out), time.time() - t_start))
    else:
        with multiprocessing.Pool(jobs) as pool:
            for i, res in enumerate(pool.imap_unordered(parse_thread_worker, tasks, chunksize=1), 1):
                absorb(res)
                if i % 20 == 0 or i == len(tasks):
                    print("  [%3d/%d] %d postings so far  (%.0fs)"
                          % (i, len(tasks), len(postings_out), time.time() - t_start))
    postings_out.sort(key=lambda o: (o["m"], o["id"]))
    print("  parsed in %.0fs" % (time.time() - t_start))

    # ---- write jsonl ----------------------------------------------------------
    # EVERY parsed posting is written -- no row is ever dropped.  The only thing that
    # can shrink is the excerpt field 'x', and only under this explicit ladder, whose
    # every step is printed.  The full text always stays reproducible from
    # raw/hn/{thread_id}.json via the comment id.
    OUT_POSTINGS.parent.mkdir(parents=True, exist_ok=True)
    ladder = []
    chosen = EXCERPT_STEPS[-1]
    for step in EXCERPT_STEPS:
        with open(OUT_POSTINGS, "w", encoding="utf-8") as f:
            for o in postings_out:
                if step:
                    o["x"] = o["x"][:step]
                else:
                    o.pop("x", None)
                f.write(json.dumps(o, separators=(",", ":"), ensure_ascii=False))
                f.write("\n")
        size = OUT_POSTINGS.stat().st_size
        ladder.append((step, size))
        if size <= MAX_JSONL_BYTES:
            chosen = step
            break
    jl_mb = OUT_POSTINGS.stat().st_size / 1e6
    print("\nwrote data/hn_postings.jsonl  (%.1f MB, %d rows -- no rows dropped)"
          % (jl_mb, len(postings_out)))
    print("  excerpt-length ladder (budget %.0f MB):" % (MAX_JSONL_BYTES / 1e6))
    for step, size in ladder:
        print("    x=%3d chars -> %5.1f MB  %s"
              % (step, size / 1e6, "ACCEPTED" if step == chosen else "over budget, shrinking"))
    if chosen != EXCERPT_STEPS[0]:
        print("  NOTE: excerpts were shortened from %d to %d chars to stay under budget; "
              "no posting and no field other than 'x' was affected."
              % (EXCERPT_STEPS[0], chosen))

    # ---- aggregate ------------------------------------------------------------
    months = sorted(months_seen)
    years = sorted({m[:4] for m in months})
    mi = {m: i for i, m in enumerate(months)}
    yi = {y: i for i, y in enumerate(years)}
    nm, ny = len(months), len(years)
    RC = ["remote", "hybrid", "onsite", "unknown"]

    tot_m = [0] * nm
    tot_y = [0] * ny
    rc_m = {k: [0] * nm for k in RC}
    rc_y = {k: [0] * ny for k in RC}
    tech_m = {t: [0] * nm for t in TECHS}
    tech_y = {t: [0] * ny for t in TECHS}
    techrem_m = {t: [0] * nm for t in TECHS}
    techrem_y = {t: [0] * ny for t in TECHS}
    role_ids = sorted(list(ROLES) + ["other-engineering", "other"])
    role_m = {r: [0] * nm for r in role_ids}
    role_y = {r: [0] * ny for r in role_ids}
    role_rc_m = {r: {k: [0] * nm for k in RC} for r in role_ids}
    role_rc_y = {r: {k: [0] * ny for k in RC} for r in role_ids}
    scope_m = defaultdict(lambda: [0] * nm)
    scope_y = defaultdict(lambda: [0] * ny)
    sen_m = defaultdict(lambda: [0] * nm)
    sen_y = defaultdict(lambda: [0] * ny)
    remote_tot_m = [0] * nm
    remote_tot_y = [0] * ny
    sal_m = [[] for _ in range(nm)]
    visa_m = [0] * nm
    visa_den_m = [0] * nm

    for o in postings_out:
        i, j = mi[o["m"]], yi[o["m"][:4]]
        tot_m[i] += 1
        tot_y[j] += 1
        rc = o["rc"]
        rc_m[rc][i] += 1
        rc_y[rc][j] += 1
        is_rem = rc == "remote"
        if is_rem:
            remote_tot_m[i] += 1
            remote_tot_y[j] += 1
        for t in o.get("tc", []):
            tech_m[t][i] += 1
            tech_y[t][j] += 1
            if is_rem:
                techrem_m[t][i] += 1
                techrem_y[t][j] += 1
        for r in o.get("r", ["other"]):
            role_m[r][i] += 1
            role_y[r][j] += 1
            role_rc_m[r][rc][i] += 1
            role_rc_y[r][rc][j] += 1
        if o.get("rs"):
            scope_m[o["rs"]][i] += 1
            scope_y[o["rs"]][j] += 1
        sen_m[o.get("sr", "unknown")][i] += 1
        sen_y[o.get("sr", "unknown")][j] += 1
        if o.get("cur") == "USD" and o.get("smin") and o.get("smax"):
            sal_m[i].append((o["smin"] + o["smax"]) / 2)
        if o.get("v") is not None:
            visa_den_m[i] += 1
            if o["v"]:
                visa_m[i] += 1

    def share(nums, dens):
        return [round(n / d, 5) if d else None for n, d in zip(nums, dens)]

    def median(xs):
        if not xs:
            return None
        xs = sorted(xs)
        n = len(xs)
        return round((xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2))

    expected = manifest["months_expected"]
    missing = [m for m in expected if m not in months_seen]

    trends = {
        "meta": {
            "source": "Hacker News 'Ask HN: Who is hiring?' via hn.algolia.com/api/v1",
            "generated_months": months,
            "years": years,
            "threads_fetched": len(threads),
            "thread_type_counts_seen": manifest["thread_type_counts"],
            "months_expected": expected,
            "months_missing": missing,
            "comments_in": counters["comments_in"],
            "postings_parsed": counters["postings"],
            "postings_deleted_or_empty": counters["deleted_or_empty"],
            # NOT "non-postings": this is the count the is_posting heuristic could not
            # confirm as a job ad.  Hand-reading 55 random ones (>=60 chars) after the fix
            # put ~1/3 of them at real job ads, so per-year posting counts are a floor.
            "postings_dropped_unconfirmed": counters["non_posting"],
            "postings_dropped_unconfirmed_by_year": {
                y: {"comments": counters["cy_" + y], "dropped": counters.get("dy_" + y, 0),
                    "rate": round(counters.get("dy_" + y, 0) / max(1, counters["cy_" + y]), 4)}
                for y in years},
            "remote_decision_tier": {k[5:]: v for k, v in counters.items() if k.startswith("tier_")},
            "role_source": {k[8:]: v for k, v in counters.items() if k.startswith("rolesrc_")},
            "remote_classifier_accuracy": {
                "curated_hard_cases": acc_curated,
                "heldout_blind_sample": acc_heldout,
                "note": ("heldout_blind_sample is the honest generalisation estimate: 42 header "
                         "strings drawn at random (one comment per thread, seed 20260902) and "
                         "hand-labelled BEFORE the classifier was run on them. curated_hard_cases "
                         "was assembled after reading the corpus and only verifies that the "
                         "intended rules fire."),
            },
            # Result of manually reading `--audit` output: 20 accepted matches were sampled
            # (reservoir, corpus-wide) for each ambiguous token and eyeballed one by one.
            "technology_disambiguation_audit": {
                "method": ("uv run python pipeline/parse_hn.py --audit --audit-n 20 prints 20 "
                           "randomly sampled ACCEPTED matches per ambiguous token with +-70 chars "
                           "of context; each was hand-checked for whether it really refers to the "
                           "technology. Re-runnable; these figures come from two passes."),
                "pass_1_before_tuning": {
                    "samples_read": 369, "false_positives": 15, "fp_rate": 0.041,
                    "per_token_fp": {"c": "10/20 (Objective-C and Obj-C read as C)",
                                     "lambda": "3/20 (Lambda Labs the GPU cloud; 'Java 8 lambda')",
                                     "phoenix": "1/20 (Phoenix, Arizona in an office list)",
                                     "spark": "1/20 (Spark Java the web framework)",
                                     "all_others": "0/20 each"},
                },
                "fixes_applied": [
                    "c: anti-patterns for objective-c / obj-c / objc (accepted matches 2790 -> 1861)",
                    "lambda: requires an AWS/serverless token within 250 chars (1001 -> 931)",
                    "phoenix: requires an Elixir/BEAM token within 250 chars (552 -> 486)",
                    "spark: anti-pattern for 'Spark Java' / 'SparkJava' (2020 -> 2019)",
                ],
                "pass_2_after_tuning": {
                    "samples_read": 379, "false_positives": 1, "fp_rate": 0.003,
                    "residual": ("1 case: 'Backend web frameworks (e.g. Spring Boot, Spark or "
                                 "DropWizard)' is Spark Java, still counted as Apache Spark. "
                                 "Measured residual FP for the `spark` token alone is ~5%."),
                    "clean_tokens": ["c", "dart", "dbt", "express", "go", "julia", "lambda", "nim",
                                     "nomad", "phoenix", "r", "rag", "rails", "ruby", "rust",
                                     "spring", "swift", "tailwind", "zig"],
                },
            },
            "postings_jsonl_schema": {
                "id": "HN comment id", "m": "YYYY-MM thread month", "a": "HN author",
                "rc": "remote_class: remote|hybrid|onsite|unknown", "rs": "remote_scope or absent",
                "co": "company (best effort)", "t": "title/role line", "loc": "location string",
                "sr": "seniority (absent when unknown)", "r": "role ids (absent when only 'other')",
                "tc": "technology ids", "smin": "salary min", "smax": "salary max",
                "cur": "salary currency", "v": "visa sponsorship bool", "rel": "relocation bool",
                "x": ("first %d chars of the cleaned posting text (absent when 0)" % chosen),
            },
            "postings_excerpt_chars": chosen,
            "postings_rows": len(postings_out),
            "postings_size_ladder": [{"excerpt_chars": st, "bytes": sz} for st, sz in ladder],
            "notes": [
                "One top-level comment = one posting; replies (grandchildren) are ignored.",
                "is_posting() is a RECALL-tuned heuristic; meta.postings_dropped_unconfirmed "
                "counts comments it could not confirm as job ads (thread meta-chatter, "
                "interview complaints, job-board plugs -- plus a minority of real ads with no "
                "hiring vocabulary at all). Hand-read samples: 55 of the %d drops read, ~1/3 "
                "are real ads (~0.16%% of all comments); 65 of the 1963 comments the previous "
                "buggy filter wrongly dropped read, 6 (9%%) are not ads. Per-year drop rates "
                "are in postings_dropped_unconfirmed_by_year; posting counts are a floor, "
                "tightest in 2011 (1.5%% of that year's comments unconfirmed). Reproduce with: "
                "parse_hn.py --filter-audit 25." % counters["non_posting"],
                "Every parsed posting is in the jsonl -- no row is sampled away or capped.",
                "Raw posting text is NOT shipped; 'x' is a %d-char excerpt. Full text stays "
                "reproducible from raw/hn/{thread_id}.json via the comment id." % chosen,
                "Technology shares are share-of-postings-that-month, not share-of-mentions: a "
                "posting counts once for a technology however many times it names it.",
                "tech_counts_remote / year_shares_remote count only postings classified remote.",
                "remote_scope is a single label with the precedence documented in SCOPE_RULES.",
                "Roles are matched on the header line first and only fall back to the body when "
                "the header yields nothing; meta.role_source reports how often each path was used.",
            ],
        },
        "months": months,
        "years": years,
        "totals": {"month": tot_m, "year": tot_y},
        "remote": {
            "month_counts": rc_m,
            "month_shares": {k: share(rc_m[k], tot_m) for k in RC},
            "year_counts": rc_y,
            "year_shares": {k: share(rc_y[k], tot_y) for k in RC},
        },
        "remote_scope": {"month_counts": {k: v for k, v in sorted(scope_m.items())},
                         "year_counts": {k: v for k, v in sorted(scope_y.items())}},
        "seniority": {"month_counts": dict(sorted(sen_m.items())),
                      "year_counts": dict(sorted(sen_y.items()))},
        "tech": {
            "ids": sorted(TECHS),
            "labels": {t: TECHS[t]["label"] for t in sorted(TECHS)},
            "categories": {t: TECHS[t]["category"] for t in sorted(TECHS)},
            "month_counts": {t: tech_m[t] for t in sorted(TECHS)},
            "month_shares": {t: share(tech_m[t], tot_m) for t in sorted(TECHS)},
            "year_counts": {t: tech_y[t] for t in sorted(TECHS)},
            "year_shares": {t: share(tech_y[t], tot_y) for t in sorted(TECHS)},
            "month_counts_remote": {t: techrem_m[t] for t in sorted(TECHS)},
            "year_counts_remote": {t: techrem_y[t] for t in sorted(TECHS)},
            "year_shares_remote": {t: share(techrem_y[t], remote_tot_y) for t in sorted(TECHS)},
        },
        "role": {
            "ids": role_ids,
            "labels": dict([(r, ROLES[r]["label"]) for r in ROLES]
                           + [("other-engineering", "Other engineering"), ("other", "Other / unclassified")]),
            "month_counts": role_m,
            "month_shares": {r: share(role_m[r], tot_m) for r in role_ids},
            "year_counts": role_y,
            "year_shares": {r: share(role_y[r], tot_y) for r in role_ids},
            "month_counts_by_remote": role_rc_m,
            "year_counts_by_remote": role_rc_y,
        },
        "remote_totals": {"month": remote_tot_m, "year": remote_tot_y},
        "salary_usd_median": {"month": [median(x) for x in sal_m]},
        "visa": {"month_yes": visa_m, "month_stated": visa_den_m},
    }
    write_json(trends, OUT_TRENDS)

    tax = {tid: {"label": s["label"], "category": s["category"], "patterns": s["patterns"],
                 "anti_patterns": s["anti_patterns"], "ambiguous": s["ambiguous"],
                 "requires": s["requires"]}
           for tid, s in sorted(TECHS.items())}
    # role taxonomy rides along in the same file under reserved underscore keys
    tax["_roles"] = {rid: {"label": s["label"], "patterns": s["patterns"]}
                     for rid, s in sorted(ROLES.items())}
    tax["_roles"]["other-engineering"] = {"label": "Other engineering",
                                          "patterns": [GENERIC_ENG_RE.pattern]}
    tax["_roles"]["other"] = {"label": "Other / unclassified", "patterns": []}
    tax["_qualifier_pattern"] = QUALIFIER_RE.pattern
    tax["_notes"] = [
        "Every pattern is a Python regex applied case-INSENSITIVELY unless a part of it is "
        "wrapped in (?-i:...), which makes that part case-sensitive.",
        "A technology is accepted when one of its `patterns` matches and no `anti_patterns` "
        "match overlaps that match.",
        "A technology flagged `ambiguous` additionally needs, within %d characters of the "
        "surviving match, either an accepted unambiguous technology or a word from "
        "_qualifier_pattern." % CONTEXT_WINDOW,
        "A technology with a non-null `requires` additionally needs that regex to match within "
        "%d characters of the match ('Phoenix' only counts near Elixir; 'Lambda' only near AWS)."
        % REQUIRE_WINDOW,
        "Matching runs on the cleaned posting text with URLs stripped, so a technology name that "
        "only appears inside a link does not count.",
        "A posting counts at most once per technology however many times it names it.",
    ]
    write_json(tax, OUT_TAXONOMY, compact=False)

    # ---- summary --------------------------------------------------------------
    print("\n" + "=" * 72)
    print("ROWS IN / OUT")
    print("  top-level comments read : %d" % counters["comments_in"])
    print("  deleted / empty         : %d" % counters["deleted_or_empty"])
    print("  dropped, not confirmed  : %d  (is_posting() could not confirm these as job ads;"
          % counters["non_posting"])
    print("                            a hand-read sample says ~1/3 of them still are)")
    print("  postings written        : %d" % counters["postings"])
    print("  months covered          : %d  (%s .. %s)" % (nm, months[0], months[-1]))
    print("  months expected         : %d" % len(expected))
    print("  months MISSING          : %s" % (missing if missing else "none"))
    print("\nPER-YEAR COVERAGE")
    for y in years:
        idx = [i for i, m in enumerate(months) if m[:4] == y]
        print("    %s  months=%2d  postings=%6d  unconfirmed-dropped=%4d (%4.1f%%)  "
              "remote=%5.1f%%  hybrid=%4.1f%%  onsite=%5.1f%%  unknown=%4.1f%%"
              % (y, len(idx), tot_y[yi[y]], counters.get("dy_" + y, 0),
                 100.0 * counters.get("dy_" + y, 0) / max(1, counters["cy_" + y]),
                 100.0 * rc_y["remote"][yi[y]] / max(1, tot_y[yi[y]]),
                 100.0 * rc_y["hybrid"][yi[y]] / max(1, tot_y[yi[y]]),
                 100.0 * rc_y["onsite"][yi[y]] / max(1, tot_y[yi[y]]),
                 100.0 * rc_y["unknown"][yi[y]] / max(1, tot_y[yi[y]])))

    latest_year = years[-1]
    base_year = "2018" if "2018" in yi else years[0]
    li, bi = yi[latest_year], yi[base_year]
    rows = sorted(TECHS, key=lambda t: -(tech_y[t][li] / max(1, tot_y[li])))
    print("\nTOP 30 TECHNOLOGIES BY %s SHARE-OF-POSTINGS (vs %s)" % (latest_year, base_year))
    print("  %-24s %9s %9s %9s   %s" % ("technology", latest_year, base_year, "delta", "n_%s" % latest_year))
    for t in rows[:30]:
        s_new = tech_y[t][li] / max(1, tot_y[li]) * 100
        s_old = tech_y[t][bi] / max(1, tot_y[bi]) * 100
        print("  %-24s %8.2f%% %8.2f%% %+8.2f   %6d"
              % (TECHS[t]["label"][:24], s_new, s_old, s_new - s_old, tech_y[t][li]))

    print("\nBIGGEST RISERS / FALLERS %s -> %s (share-of-postings pp, techs with n>=40 in either year)"
          % (base_year, latest_year))
    cand = [t for t in TECHS if tech_y[t][li] >= 40 or tech_y[t][bi] >= 40]
    delta = sorted(cand, key=lambda t: -((tech_y[t][li] / max(1, tot_y[li])) - (tech_y[t][bi] / max(1, tot_y[bi]))))
    for t in delta[:12]:
        print("   up   %-24s %+6.2f pp  (%.2f%% -> %.2f%%)"
              % (TECHS[t]["label"][:24],
                 (tech_y[t][li] / max(1, tot_y[li]) - tech_y[t][bi] / max(1, tot_y[bi])) * 100,
                 tech_y[t][bi] / max(1, tot_y[bi]) * 100, tech_y[t][li] / max(1, tot_y[li]) * 100))
    for t in delta[-12:]:
        print("   down %-24s %+6.2f pp  (%.2f%% -> %.2f%%)"
              % (TECHS[t]["label"][:24],
                 (tech_y[t][li] / max(1, tot_y[li]) - tech_y[t][bi] / max(1, tot_y[bi])) * 100,
                 tech_y[t][bi] / max(1, tot_y[bi]) * 100, tech_y[t][li] / max(1, tot_y[li]) * 100))

    print("\nOUTPUT SIZES")
    for p in (OUT_POSTINGS, OUT_TRENDS, OUT_TAXONOMY):
        print("  %-28s %8.2f MB" % (p.name, p.stat().st_size / 1e6))
    print("=" * 72)

    # ---- QA dumps (last, so a console-encoding hiccup can never lose the outputs) ----
    if args.sample_headers:
        print("\nRANDOM REAL HEADERS (month | class | deciding tier | header)")
        for mo, rc, tier, h in sorted(header_samples):
            print("  %s  %-7s %-11s %s" % (mo, rc, tier, h))
    if args.audit:
        print("\n" + "=" * 72)
        print("AMBIGUOUS TOKEN AUDIT -- up to %d accepted matches each, +-70 chars of context."
              % args.audit_n)
        print("Read these to measure the false-positive rate of each disambiguated token.")
        for tid in sorted(AMBIGUOUS_IDS):
            smp = audit_samples.get(tid, [])
            print("\n--- %s (%s) : %d sampled of %d accepted matches"
                  % (tid, TECHS[tid]["label"], len(smp), audit_seen.get(tid, 0)))
            for mo, ctx in smp:
                print("   [%s] %s" % (mo, ctx[:170]))
        print("=" * 72)


if __name__ == "__main__":
    main()
