"""Shared G-SECRETS runner: release range, candidate tree and runtime controls.

Standard library only. The scanner writes JSON reports into a private
scratch directory; public output is fixed phase lines, sanitized rule
counts and fixed diagnostics. Reports and scanner streams never reach
the console and are removed with the scratch directory.
"""

import base64
import collections
import json
import math
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

GITLEAKS_VERSION = "v8.18.4"
GITLEAKS_SHA256 = (
    "ba6dbb656933921c775ee5a2d1c13a91"
    "046e7952e9d919f9bac4cec61d628e7d"
)
GITLEAKS_URL = (
    "https://github.com/gitleaks/gitleaks/releases/download/"
    "v8.18.4/gitleaks_8.18.4_linux_x64.tar.gz"
)
# [TARGET, unit decision] Constant policy, independent of repository config.
# Companion rules implement path AND secret scoping on pinned gitleaks v8.18.4.
CONFIG_TEXT = r"""# [TARGET, unit decision] Preserve default detection outside two digest files.
# v8.18.4 allowlist paths and regexes use OR. Companion rules restore detection
# in those files and exempt only an exact lowercase SHA-256 secret there.
[extend]
useDefault = true

[[rules]]
id = "facebook-page-access-token"
description = "Discovered a Facebook Page Access Token, posing a risk of unauthorized access to Facebook accounts and personal data exposure."
regex = '''(?i)\b(EAA[MC][a-z0-9]{20,})(?:['|\"|\n|\r|\s|\x60|;]|$)'''
keywords = ["eaam", "eaac"]
[rules.allowlist]
paths = ['''^(catalogue/router/manifest\.json|router/index/SHA256SUMS)$''']

[[rules]]
id = "facebook-page-access-token-generated-digest-files"
description = "Discovered a Facebook Page Access Token, posing a risk of unauthorized access to Facebook accounts and personal data exposure."
regex = '''(?i)\b(EAA[MC][a-z0-9]{20,})(?:['|\"|\n|\r|\s|\x60|;]|$)'''
keywords = ["eaam", "eaac"]
secretGroup = 1
path = '''^(catalogue/router/manifest\.json|router/index/SHA256SUMS)$'''
[rules.allowlist]
regexTarget = "secret"
regexes = ['''^[0-9a-f]{64}$''']

[[rules]]
id = "square-access-token"
description = "Detected a Square Access Token, risking unauthorized payment processing and financial transaction exposure."
regex = '''(?i)\b((EAAA|sq0atp-)[0-9A-Za-z\-_]{22,60})(?:['|\"|\n|\r|\s|\x60|;]|$)'''
keywords = ["sq0atp-", "eaaa"]
[rules.allowlist]
paths = ['''^(catalogue/router/manifest\.json|router/index/SHA256SUMS)$''']

[[rules]]
id = "square-access-token-generated-digest-files"
description = "Detected a Square Access Token, risking unauthorized payment processing and financial transaction exposure."
regex = '''(?i)\b((EAAA|sq0atp-)[0-9A-Za-z\-_]{22,60})(?:['|\"|\n|\r|\s|\x60|;]|$)'''
keywords = ["sq0atp-", "eaaa"]
secretGroup = 1
path = '''^(catalogue/router/manifest\.json|router/index/SHA256SUMS)$'''
[rules.allowlist]
regexTarget = "secret"
regexes = ['''^[0-9a-f]{64}$''']

# v8.18.4 combines allowlist paths/regexes with OR. Split this one rule
# by path to exempt only the two exact recipe digests, preserving all defaults.

[[rules]]
id = "generic-api-key"
description = "Detected a Generic API Key, potentially exposing access to various services and sensitive operations."
regex = "(?i)(?:key|api|token|secret|client|passwd|password|auth|access)(?:[0-9a-z\\-_\\t .]{0,20})(?:[\\s|']|[\\s|\"]){0,3}(?:=|>|:{1,3}=|\\|\\|:|<=|=>|:|\\?=)(?:'|\\\"|\\s|=|\\x60){0,5}([0-9a-z\\-_.=]{10,150})(?:['|\\\"|\\n|\\r|\\s|\\x60|;]|$)"
entropy = 3.5
keywords = ["key", "api", "token", "secret", "client", "passwd", "password", "auth", "access"]
secretGroup = 1
[rules.allowlist]
stopwords = ["000000", "aaaaaa", "about", "abstract", "academy", "acces", "account", "act-", "act.", "act_", "action", "active", "actively", "activity", "adapter", "add-", "add.", "add_", "add-on", "addon", "addres", "admin", "adobe", "advanced", "adventure", "agent", "agile", "air-", "air.", "air_", "ajax", "akka", "alert", "alfred", "algorithm", "all-", "all.", "all_", "alloy", "alpha", "amazon", "amqp", "analysi", "analytic", "analyzer", "android", "angular", "angularj", "animate", "animation", "another", "ansible", "answer", "ant-", "ant.", "ant_", "any-", "any.", "any_", "apache", "app-", "app-", "app.", "app.", "app_", "app_", "apple", "arch", "archive", "archived", "arduino", "array", "art-", "art.", "art_", "article", "asp-", "asp.", "asp_", "asset", "async", "atom", "attention", "audio", "audit", "aura", "auth", "author", "author", "authorize", "auto", "automated", "automatic", "awesome", "aws_", "azure", "back", "backbone", "backend", "backup", "bar-", "bar.", "bar_", "base", "based", "bash", "basic", "batch", "been", "beer", "behavior", "being", "benchmark", "best", "beta", "better", "big-", "big.", "big_", "binary", "binding", "bit-", "bit.", "bit_", "bitcoin", "block", "blog", "board", "book", "bookmark", "boost", "boot", "bootstrap", "bosh", "bot-", "bot.", "bot_", "bower", "box-", "box.", "box_", "boxen", "bracket", "branch", "bridge", "browser", "brunch", "buffer", "bug-", "bug.", "bug_", "build", "builder", "building", "buildout", "buildpack", "built", "bundle", "busines", "but-", "but.", "but_", "button", "cache", "caching", "cakephp", "calendar", "call", "camera", "campfire", "can-", "can.", "can_", "canva", "captcha", "capture", "card", "carousel", "case", "cassandra", "cat-", "cat.", "cat_", "category", "center", "cento", "challenge", "change", "changelog", "channel", "chart", "chat", "cheat", "check", "checker", "chef", "ches", "chinese", "chosen", "chrome", "ckeditor", "clas", "classe", "classic", "clean", "cli-", "cli.", "cli_", "client", "client", "clojure", "clone", "closure", "cloud", "club", "cluster", "cms-", "cms_", "coco", "code", "coding", "coffee", "color", "combination", "combo", "command", "commander", "comment", "commit", "common", "community", "compas", "compiler", "complete", "component", "composer", "computer", "computing", "con-", "con.", "con_", "concept", "conf", "config", "config", "connect", "connector", "console", "contact", "container", "contao", "content", "contest", "context", "control", "convert", "converter", "conway'", "cookbook", "cookie", "cool", "copy", "cordova", "core", "couchbase", "couchdb", "countdown", "counter", "course", "craft", "crawler", "create", "creating", "creator", "credential", "crm-", "crm.", "crm_", "cros", "crud", "csv-", "csv.", "csv_", "cube", "cucumber", "cuda", "current", "currently", "custom", "daemon", "dark", "dart", "dash", "dashboard", "data", "database", "date", "day-", "day.", "day_", "dead", "debian", "debug", "debug", "debugger", "deck", "define", "del-", "del.", "del_", "delete", "demo", "deploy", "design", "designer", "desktop", "detection", "detector", "dev-", "dev.", "dev_", "develop", "developer", "device", "devise", "diff", "digital", "directive", "directory", "discovery", "display", "django", "dns-", "dns_", "doc-", "doc-", "doc.", "doc.", "doc_", "doc_", "docker", "docpad", "doctrine", "document", "doe-", "doe.", "doe_", "dojo", "dom-", "dom.", "dom_", "domain", "done", "don't", "dot-", "dot.", "dot_", "dotfile", "download", "draft", "drag", "drill", "drive", "driven", "driver", "drop", "dropbox", "drupal", "dsl-", "dsl.", "dsl_", "dynamic", "easy", "_ec2_", "ecdsa", "eclipse", "edit", "editing", "edition", "editor", "element", "emac", "email", "embed", "embedded", "ember", "emitter", "emulator", "encoding", "endpoint", "engine", "english", "enhanced", "entity", "entry", "env_", "episode", "erlang", "error", "espresso", "event", "evented", "example", "example", "exchange", "exercise", "experiment", "expire", "exploit", "explorer", "export", "exporter", "expres", "ext-", "ext.", "ext_", "extended", "extension", "external", "extra", "extractor", "fabric", "facebook", "factory", "fake", "fast", "feature", "feed", "fewfwef", "ffmpeg", "field", "file", "filter", "find", "finder", "firefox", "firmware", "first", "fish", "fix-", "fix_", "flash", "flask", "flat", "flex", "flexible", "flickr", "flow", "fluent", "fluentd", "fluid", "folder", "font", "force", "foreman", "fork", "form", "format", "formatter", "forum", "foundry", "framework", "free", "friend", "friendly", "front-end", "frontend", "ftp-", "ftp.", "ftp_", "fuel", "full", "fun-", "fun.", "fun_", "func", "future", "gaia", "gallery", "game", "gateway", "gem-", "gem.", "gem_", "gen-", "gen.", "gen_", "general", "generator", "generic", "genetic", "get-", "get.", "get_", "getenv", "getting", "ghost", "gist", "git-", "git.", "git_", "github", "gitignore", "gitlab", "glas", "gmail", "gnome", "gnu-", "gnu.", "gnu_", "goal", "golang", "gollum", "good", "google", "gpu-", "gpu.", "gpu_", "gradle", "grail", "graph", "graphic", "great", "grid", "groovy", "group", "grunt", "guard", "gui-", "gui.", "gui_", "guide", "guideline", "gulp", "gwt-", "gwt.", "gwt_", "hack", "hackathon", "hacker", "hacking", "hadoop", "haml", "handler", "hardware", "has-", "has_", "hash", "haskell", "have", "haxe", "hello", "help", "helper", "here", "hero", "heroku", "high", "hipchat", "history", "home", "homebrew", "homepage", "hook", "host", "hosting", "hot-", "hot.", "hot_", "house", "how-", "how.", "how_", "html", "http", "hub-", "hub.", "hub_", "hubot", "human", "icon", "ide-", "ide.", "ide_", "idea", "identity", "idiomatic", "image", "impact", "import", "important", "importer", "impres", "index", "infinite", "info", "injection", "inline", "input", "inside", "inspector", "instagram", "install", "installer", "instant", "intellij", "interface", "internet", "interview", "into", "intro", "ionic", "iphone", "ipython", "irc-", "irc_", "iso-", "iso.", "iso_", "issue", "jade", "jasmine", "java", "jbos", "jekyll", "jenkin", "job-", "job.", "job_", "joomla", "jpa-", "jpa.", "jpa_", "jquery", "json", "just", "kafka", "karma", "kata", "kernel", "keyboard", "kindle", "kit-", "kit.", "kit_", "kitchen", "knife", "koan", "kohana", "lab-", "lab-", "lab.", "lab.", "lab_", "lab_", "lambda", "lamp", "language", "laravel", "last", "latest", "latex", "launcher", "layer", "layout", "lazy", "ldap", "leaflet", "league", "learn", "learning", "led-", "led.", "led_", "leetcode", "les-", "les.", "les_", "level", "leveldb", "lib-", "lib.", "lib_", "librarie", "library", "license", "life", "liferay", "light", "lightbox", "like", "line", "link", "linked", "linkedin", "linux", "lisp", "list", "lite", "little", "load", "loader", "local", "location", "lock", "log-", "log.", "log_", "logger", "logging", "logic", "login", "logstash", "longer", "look", "love", "lua-", "lua.", "lua_", "mac-", "mac.", "mac_", "machine", "made", "magento", "magic", "mail", "make", "maker", "making", "man-", "man.", "man_", "manage", "manager", "manifest", "manual", "map-", "map-", "map.", "map.", "map_", "map_", "mapper", "mapping", "markdown", "markup", "master", "math", "matrix", "maven", "md5", "mean", "media", "mediawiki", "meetup", "memcached", "memory", "menu", "merchant", "message", "messaging", "meta", "metadata", "meteor", "method", "metric", "micro", "middleman", "migration", "minecraft", "miner", "mini", "minimal", "mirror", "mit-", "mit.", "mit_", "mobile", "mocha", "mock", "mod-", "mod.", "mod_", "mode", "model", "modern", "modular", "module", "modx", "money", "mongo", "mongodb", "mongoid", "mongoose", "monitor", "monkey", "more", "motion", "moved", "movie", "mozilla", "mqtt", "mule", "multi", "multiple", "music", "mustache", "mvc-", "mvc.", "mvc_", "mysql", "nagio", "name", "native", "need", "neo-", "neo.", "neo_", "nest", "nested", "net-", "net.", "net_", "nette", "network", "new-", "new-", "new.", "new.", "new_", "new_", "next", "nginx", "ninja", "nlp-", "nlp.", "nlp_", "node", "nodej", "nosql", "not-", "not.", "not_", "note", "notebook", "notepad", "notice", "notifier", "now-", "now.", "now_", "number", "oauth", "object", "objective", "obsolete", "ocaml", "octopres", "official", "old-", "old.", "old_", "onboard", "online", "only", "open", "opencv", "opengl", "openshift", "openwrt", "option", "oracle", "org-", "org.", "org_", "origin", "original", "orm-", "orm.", "orm_", "osx-", "osx_", "our-", "our.", "our_", "out-", "out.", "out_", "output", "over", "overview", "own-", "own.", "own_", "pack", "package", "packet", "page", "page", "panel", "paper", "paperclip", "para", "parallax", "parallel", "parse", "parser", "parsing", "particle", "party", "password", "patch", "path", "pattern", "payment", "paypal", "pdf-", "pdf.", "pdf_", "pebble", "people", "perl", "personal", "phalcon", "phoenix", "phone", "phonegap", "photo", "php-", "php.", "php_", "physic", "picker", "pipeline", "platform", "play", "player", "please", "plu-", "plu.", "plu_", "plug-in", "plugin", "plupload", "png-", "png.", "png_", "poker", "polyfill", "polymer", "pool", "pop-", "pop.", "pop_", "popcorn", "popup", "port", "portable", "portal", "portfolio", "post", "power", "powered", "powerful", "prelude", "pretty", "preview", "principle", "print", "pro-", "pro.", "pro_", "problem", "proc", "product", "profile", "profiler", "program", "progres", "project", "protocol", "prototype", "provider", "proxy", "public", "pull", "puppet", "pure", "purpose", "push", "pusher", "pyramid", "python", "quality", "query", "queue", "quick", "rabbitmq", "rack", "radio", "rail", "railscast", "random", "range", "raspberry", "rdf-", "rdf.", "rdf_", "react", "reactive", "read", "reader", "readme", "ready", "real", "reality", "real-time", "realtime", "recipe", "recorder", "red-", "red.", "red_", "reddit", "redi", "redmine", "reference", "refinery", "refresh", "registry", "related", "release", "remote", "rendering", "repo", "report", "request", "require", "required", "requirej", "research", "resource", "response", "resque", "rest", "restful", "resume", "reveal", "reverse", "review", "riak", "rich", "right", "ring", "robot", "role", "room", "router", "routing", "rpc-", "rpc.", "rpc_", "rpg-", "rpg.", "rpg_", "rspec", "ruby-", "ruby.", "ruby_", "rule", "run-", "run.", "run_", "runner", "running", "runtime", "rust", "rvm-", "rvm.", "rvm_", "salt", "sample", "sample", "sandbox", "sas-", "sas.", "sas_", "sbt-", "sbt.", "sbt_", "scala", "scalable", "scanner", "schema", "scheme", "school", "science", "scraper", "scratch", "screen", "script", "scroll", "scs-", "scs.", "scs_", "sdk-", "sdk.", "sdk_", "sdl-", "sdl.", "sdl_", "search", "secure", "security", "see-", "see.", "see_", "seed", "select", "selector", "selenium", "semantic", "sencha", "send", "sentiment", "serie", "server", "service", "session", "set-", "set.", "set_", "setting", "setting", "setup", "sha1", "sha2", "sha256", "share", "shared", "sharing", "sheet", "shell", "shield", "shipping", "shop", "shopify", "shortener", "should", "show", "showcase", "side", "silex", "simple", "simulator", "single", "site", "skeleton", "sketch", "skin", "slack", "slide", "slider", "slim", "small", "smart", "smtp", "snake", "snippet", "soap", "social", "socket", "software", "solarized", "solr", "solution", "solver", "some", "soon", "source", "space", "spark", "spatial", "spec", "sphinx", "spine", "spotify", "spree", "spring", "sprite", "sql-", "sql.", "sql_", "sqlite", "ssh-", "ssh.", "ssh_", "stack", "staging", "standard", "stanford", "start", "started", "starter", "startup", "stat", "statamic", "state", "static", "statistic", "statsd", "statu", "steam", "step", "still", "stm-", "stm.", "stm_", "storage", "store", "storm", "story", "strategy", "stream", "streaming", "string", "stripe", "structure", "studio", "study", "stuff", "style", "sublime", "sugar", "suite", "summary", "super", "support", "supported", "svg-", "svg.", "svg_", "svn-", "svn.", "svn_", "swagger", "swift", "switch", "switcher", "symfony", "symphony", "sync", "synopsi", "syntax", "system", "system", "tab-", "tab-", "tab.", "tab.", "tab_", "tab_", "table", "tag-", "tag-", "tag.", "tag.", "tag_", "tag_", "talk", "target", "task", "tcp-", "tcp.", "tcp_", "tdd-", "tdd.", "tdd_", "team", "tech", "template", "term", "terminal", "testing", "tetri", "text", "textmate", "theme", "theory", "three", "thrift", "time", "timeline", "timer", "tiny", "tinymce", "tip-", "tip.", "tip_", "title", "todo", "todomvc", "token", "tool", "toolbox", "toolkit", "top-", "top.", "top_", "tornado", "touch", "tower", "tracker", "tracking", "traffic", "training", "transfer", "translate", "transport", "tree", "trello", "try-", "try.", "try_", "tumblr", "tut-", "tut.", "tut_", "tutorial", "tweet", "twig", "twitter", "type", "typo", "ubuntu", "uiview", "ultimate", "under", "unit", "unity", "universal", "unix", "update", "updated", "upgrade", "upload", "uploader", "uri-", "uri.", "uri_", "url-", "url.", "url_", "usage", "usb-", "usb.", "usb_", "use-", "use.", "use_", "used", "useful", "user", "using", "util", "utilitie", "utility", "vagrant", "validator", "value", "variou", "varnish", "version", "via-", "via.", "via_", "video", "view", "viewer", "vim-", "vim.", "vim_", "vimrc", "virtual", "vision", "visual", "vpn", "want", "warning", "watch", "watcher", "wave", "way-", "way.", "way_", "weather", "web-", "web_", "webapp", "webgl", "webhook", "webkit", "webrtc", "website", "websocket", "welcome", "welcome", "what", "what'", "when", "where", "which", "why-", "why.", "why_", "widget", "wifi", "wiki", "win-", "win.", "win_", "window", "wip-", "wip.", "wip_", "within", "without", "wizard", "word", "wordpres", "work", "worker", "workflow", "working", "workshop", "world", "wrapper", "write", "writer", "writing", "written", "www-", "www.", "www_", "xamarin", "xcode", "xml-", "xml.", "xml_", "xmpp", "xxxxxx", "yahoo", "yaml", "yandex", "yeoman", "yet-", "yet.", "yet_", "yii-", "yii.", "yii_", "youtube", "yui-", "yui.", "yui_", "zend", "zero", "zip-", "zip.", "zip_", "zsh-", "zsh.", "zsh_"]
paths = ["^components/OpenSuperficies/docker/vm/recipe\\.json$"]

[[rules]]
id = "generic-api-key-vm-recipe"
description = "Detected a Generic API Key, potentially exposing access to various services and sensitive operations."
regex = "(?i)(?:key|api|token|secret|client|passwd|password|auth|access)(?:[0-9a-z\\-_\\t .]{0,20})(?:[\\s|']|[\\s|\"]){0,3}(?:=|>|:{1,3}=|\\|\\|:|<=|=>|:|\\?=)(?:'|\\\"|\\s|=|\\x60){0,5}([0-9a-z\\-_.=]{10,150})(?:['|\\\"|\\n|\\r|\\s|\\x60|;]|$)"
entropy = 3.5
keywords = ["key", "api", "token", "secret", "client", "passwd", "password", "auth", "access"]
secretGroup = 1
path = "^components/OpenSuperficies/docker/vm/recipe\\.json$"
[rules.allowlist]
stopwords = ["000000", "aaaaaa", "about", "abstract", "academy", "acces", "account", "act-", "act.", "act_", "action", "active", "actively", "activity", "adapter", "add-", "add.", "add_", "add-on", "addon", "addres", "admin", "adobe", "advanced", "adventure", "agent", "agile", "air-", "air.", "air_", "ajax", "akka", "alert", "alfred", "algorithm", "all-", "all.", "all_", "alloy", "alpha", "amazon", "amqp", "analysi", "analytic", "analyzer", "android", "angular", "angularj", "animate", "animation", "another", "ansible", "answer", "ant-", "ant.", "ant_", "any-", "any.", "any_", "apache", "app-", "app-", "app.", "app.", "app_", "app_", "apple", "arch", "archive", "archived", "arduino", "array", "art-", "art.", "art_", "article", "asp-", "asp.", "asp_", "asset", "async", "atom", "attention", "audio", "audit", "aura", "auth", "author", "author", "authorize", "auto", "automated", "automatic", "awesome", "aws_", "azure", "back", "backbone", "backend", "backup", "bar-", "bar.", "bar_", "base", "based", "bash", "basic", "batch", "been", "beer", "behavior", "being", "benchmark", "best", "beta", "better", "big-", "big.", "big_", "binary", "binding", "bit-", "bit.", "bit_", "bitcoin", "block", "blog", "board", "book", "bookmark", "boost", "boot", "bootstrap", "bosh", "bot-", "bot.", "bot_", "bower", "box-", "box.", "box_", "boxen", "bracket", "branch", "bridge", "browser", "brunch", "buffer", "bug-", "bug.", "bug_", "build", "builder", "building", "buildout", "buildpack", "built", "bundle", "busines", "but-", "but.", "but_", "button", "cache", "caching", "cakephp", "calendar", "call", "camera", "campfire", "can-", "can.", "can_", "canva", "captcha", "capture", "card", "carousel", "case", "cassandra", "cat-", "cat.", "cat_", "category", "center", "cento", "challenge", "change", "changelog", "channel", "chart", "chat", "cheat", "check", "checker", "chef", "ches", "chinese", "chosen", "chrome", "ckeditor", "clas", "classe", "classic", "clean", "cli-", "cli.", "cli_", "client", "client", "clojure", "clone", "closure", "cloud", "club", "cluster", "cms-", "cms_", "coco", "code", "coding", "coffee", "color", "combination", "combo", "command", "commander", "comment", "commit", "common", "community", "compas", "compiler", "complete", "component", "composer", "computer", "computing", "con-", "con.", "con_", "concept", "conf", "config", "config", "connect", "connector", "console", "contact", "container", "contao", "content", "contest", "context", "control", "convert", "converter", "conway'", "cookbook", "cookie", "cool", "copy", "cordova", "core", "couchbase", "couchdb", "countdown", "counter", "course", "craft", "crawler", "create", "creating", "creator", "credential", "crm-", "crm.", "crm_", "cros", "crud", "csv-", "csv.", "csv_", "cube", "cucumber", "cuda", "current", "currently", "custom", "daemon", "dark", "dart", "dash", "dashboard", "data", "database", "date", "day-", "day.", "day_", "dead", "debian", "debug", "debug", "debugger", "deck", "define", "del-", "del.", "del_", "delete", "demo", "deploy", "design", "designer", "desktop", "detection", "detector", "dev-", "dev.", "dev_", "develop", "developer", "device", "devise", "diff", "digital", "directive", "directory", "discovery", "display", "django", "dns-", "dns_", "doc-", "doc-", "doc.", "doc.", "doc_", "doc_", "docker", "docpad", "doctrine", "document", "doe-", "doe.", "doe_", "dojo", "dom-", "dom.", "dom_", "domain", "done", "don't", "dot-", "dot.", "dot_", "dotfile", "download", "draft", "drag", "drill", "drive", "driven", "driver", "drop", "dropbox", "drupal", "dsl-", "dsl.", "dsl_", "dynamic", "easy", "_ec2_", "ecdsa", "eclipse", "edit", "editing", "edition", "editor", "element", "emac", "email", "embed", "embedded", "ember", "emitter", "emulator", "encoding", "endpoint", "engine", "english", "enhanced", "entity", "entry", "env_", "episode", "erlang", "error", "espresso", "event", "evented", "example", "example", "exchange", "exercise", "experiment", "expire", "exploit", "explorer", "export", "exporter", "expres", "ext-", "ext.", "ext_", "extended", "extension", "external", "extra", "extractor", "fabric", "facebook", "factory", "fake", "fast", "feature", "feed", "fewfwef", "ffmpeg", "field", "file", "filter", "find", "finder", "firefox", "firmware", "first", "fish", "fix-", "fix_", "flash", "flask", "flat", "flex", "flexible", "flickr", "flow", "fluent", "fluentd", "fluid", "folder", "font", "force", "foreman", "fork", "form", "format", "formatter", "forum", "foundry", "framework", "free", "friend", "friendly", "front-end", "frontend", "ftp-", "ftp.", "ftp_", "fuel", "full", "fun-", "fun.", "fun_", "func", "future", "gaia", "gallery", "game", "gateway", "gem-", "gem.", "gem_", "gen-", "gen.", "gen_", "general", "generator", "generic", "genetic", "get-", "get.", "get_", "getenv", "getting", "ghost", "gist", "git-", "git.", "git_", "github", "gitignore", "gitlab", "glas", "gmail", "gnome", "gnu-", "gnu.", "gnu_", "goal", "golang", "gollum", "good", "google", "gpu-", "gpu.", "gpu_", "gradle", "grail", "graph", "graphic", "great", "grid", "groovy", "group", "grunt", "guard", "gui-", "gui.", "gui_", "guide", "guideline", "gulp", "gwt-", "gwt.", "gwt_", "hack", "hackathon", "hacker", "hacking", "hadoop", "haml", "handler", "hardware", "has-", "has_", "hash", "haskell", "have", "haxe", "hello", "help", "helper", "here", "hero", "heroku", "high", "hipchat", "history", "home", "homebrew", "homepage", "hook", "host", "hosting", "hot-", "hot.", "hot_", "house", "how-", "how.", "how_", "html", "http", "hub-", "hub.", "hub_", "hubot", "human", "icon", "ide-", "ide.", "ide_", "idea", "identity", "idiomatic", "image", "impact", "import", "important", "importer", "impres", "index", "infinite", "info", "injection", "inline", "input", "inside", "inspector", "instagram", "install", "installer", "instant", "intellij", "interface", "internet", "interview", "into", "intro", "ionic", "iphone", "ipython", "irc-", "irc_", "iso-", "iso.", "iso_", "issue", "jade", "jasmine", "java", "jbos", "jekyll", "jenkin", "job-", "job.", "job_", "joomla", "jpa-", "jpa.", "jpa_", "jquery", "json", "just", "kafka", "karma", "kata", "kernel", "keyboard", "kindle", "kit-", "kit.", "kit_", "kitchen", "knife", "koan", "kohana", "lab-", "lab-", "lab.", "lab.", "lab_", "lab_", "lambda", "lamp", "language", "laravel", "last", "latest", "latex", "launcher", "layer", "layout", "lazy", "ldap", "leaflet", "league", "learn", "learning", "led-", "led.", "led_", "leetcode", "les-", "les.", "les_", "level", "leveldb", "lib-", "lib.", "lib_", "librarie", "library", "license", "life", "liferay", "light", "lightbox", "like", "line", "link", "linked", "linkedin", "linux", "lisp", "list", "lite", "little", "load", "loader", "local", "location", "lock", "log-", "log.", "log_", "logger", "logging", "logic", "login", "logstash", "longer", "look", "love", "lua-", "lua.", "lua_", "mac-", "mac.", "mac_", "machine", "made", "magento", "magic", "mail", "make", "maker", "making", "man-", "man.", "man_", "manage", "manager", "manifest", "manual", "map-", "map-", "map.", "map.", "map_", "map_", "mapper", "mapping", "markdown", "markup", "master", "math", "matrix", "maven", "md5", "mean", "media", "mediawiki", "meetup", "memcached", "memory", "menu", "merchant", "message", "messaging", "meta", "metadata", "meteor", "method", "metric", "micro", "middleman", "migration", "minecraft", "miner", "mini", "minimal", "mirror", "mit-", "mit.", "mit_", "mobile", "mocha", "mock", "mod-", "mod.", "mod_", "mode", "model", "modern", "modular", "module", "modx", "money", "mongo", "mongodb", "mongoid", "mongoose", "monitor", "monkey", "more", "motion", "moved", "movie", "mozilla", "mqtt", "mule", "multi", "multiple", "music", "mustache", "mvc-", "mvc.", "mvc_", "mysql", "nagio", "name", "native", "need", "neo-", "neo.", "neo_", "nest", "nested", "net-", "net.", "net_", "nette", "network", "new-", "new-", "new.", "new.", "new_", "new_", "next", "nginx", "ninja", "nlp-", "nlp.", "nlp_", "node", "nodej", "nosql", "not-", "not.", "not_", "note", "notebook", "notepad", "notice", "notifier", "now-", "now.", "now_", "number", "oauth", "object", "objective", "obsolete", "ocaml", "octopres", "official", "old-", "old.", "old_", "onboard", "online", "only", "open", "opencv", "opengl", "openshift", "openwrt", "option", "oracle", "org-", "org.", "org_", "origin", "original", "orm-", "orm.", "orm_", "osx-", "osx_", "our-", "our.", "our_", "out-", "out.", "out_", "output", "over", "overview", "own-", "own.", "own_", "pack", "package", "packet", "page", "page", "panel", "paper", "paperclip", "para", "parallax", "parallel", "parse", "parser", "parsing", "particle", "party", "password", "patch", "path", "pattern", "payment", "paypal", "pdf-", "pdf.", "pdf_", "pebble", "people", "perl", "personal", "phalcon", "phoenix", "phone", "phonegap", "photo", "php-", "php.", "php_", "physic", "picker", "pipeline", "platform", "play", "player", "please", "plu-", "plu.", "plu_", "plug-in", "plugin", "plupload", "png-", "png.", "png_", "poker", "polyfill", "polymer", "pool", "pop-", "pop.", "pop_", "popcorn", "popup", "port", "portable", "portal", "portfolio", "post", "power", "powered", "powerful", "prelude", "pretty", "preview", "principle", "print", "pro-", "pro.", "pro_", "problem", "proc", "product", "profile", "profiler", "program", "progres", "project", "protocol", "prototype", "provider", "proxy", "public", "pull", "puppet", "pure", "purpose", "push", "pusher", "pyramid", "python", "quality", "query", "queue", "quick", "rabbitmq", "rack", "radio", "rail", "railscast", "random", "range", "raspberry", "rdf-", "rdf.", "rdf_", "react", "reactive", "read", "reader", "readme", "ready", "real", "reality", "real-time", "realtime", "recipe", "recorder", "red-", "red.", "red_", "reddit", "redi", "redmine", "reference", "refinery", "refresh", "registry", "related", "release", "remote", "rendering", "repo", "report", "request", "require", "required", "requirej", "research", "resource", "response", "resque", "rest", "restful", "resume", "reveal", "reverse", "review", "riak", "rich", "right", "ring", "robot", "role", "room", "router", "routing", "rpc-", "rpc.", "rpc_", "rpg-", "rpg.", "rpg_", "rspec", "ruby-", "ruby.", "ruby_", "rule", "run-", "run.", "run_", "runner", "running", "runtime", "rust", "rvm-", "rvm.", "rvm_", "salt", "sample", "sample", "sandbox", "sas-", "sas.", "sas_", "sbt-", "sbt.", "sbt_", "scala", "scalable", "scanner", "schema", "scheme", "school", "science", "scraper", "scratch", "screen", "script", "scroll", "scs-", "scs.", "scs_", "sdk-", "sdk.", "sdk_", "sdl-", "sdl.", "sdl_", "search", "secure", "security", "see-", "see.", "see_", "seed", "select", "selector", "selenium", "semantic", "sencha", "send", "sentiment", "serie", "server", "service", "session", "set-", "set.", "set_", "setting", "setting", "setup", "sha1", "sha2", "sha256", "share", "shared", "sharing", "sheet", "shell", "shield", "shipping", "shop", "shopify", "shortener", "should", "show", "showcase", "side", "silex", "simple", "simulator", "single", "site", "skeleton", "sketch", "skin", "slack", "slide", "slider", "slim", "small", "smart", "smtp", "snake", "snippet", "soap", "social", "socket", "software", "solarized", "solr", "solution", "solver", "some", "soon", "source", "space", "spark", "spatial", "spec", "sphinx", "spine", "spotify", "spree", "spring", "sprite", "sql-", "sql.", "sql_", "sqlite", "ssh-", "ssh.", "ssh_", "stack", "staging", "standard", "stanford", "start", "started", "starter", "startup", "stat", "statamic", "state", "static", "statistic", "statsd", "statu", "steam", "step", "still", "stm-", "stm.", "stm_", "storage", "store", "storm", "story", "strategy", "stream", "streaming", "string", "stripe", "structure", "studio", "study", "stuff", "style", "sublime", "sugar", "suite", "summary", "super", "support", "supported", "svg-", "svg.", "svg_", "svn-", "svn.", "svn_", "swagger", "swift", "switch", "switcher", "symfony", "symphony", "sync", "synopsi", "syntax", "system", "system", "tab-", "tab-", "tab.", "tab.", "tab_", "tab_", "table", "tag-", "tag-", "tag.", "tag.", "tag_", "tag_", "talk", "target", "task", "tcp-", "tcp.", "tcp_", "tdd-", "tdd.", "tdd_", "team", "tech", "template", "term", "terminal", "testing", "tetri", "text", "textmate", "theme", "theory", "three", "thrift", "time", "timeline", "timer", "tiny", "tinymce", "tip-", "tip.", "tip_", "title", "todo", "todomvc", "token", "tool", "toolbox", "toolkit", "top-", "top.", "top_", "tornado", "touch", "tower", "tracker", "tracking", "traffic", "training", "transfer", "translate", "transport", "tree", "trello", "try-", "try.", "try_", "tumblr", "tut-", "tut.", "tut_", "tutorial", "tweet", "twig", "twitter", "type", "typo", "ubuntu", "uiview", "ultimate", "under", "unit", "unity", "universal", "unix", "update", "updated", "upgrade", "upload", "uploader", "uri-", "uri.", "uri_", "url-", "url.", "url_", "usage", "usb-", "usb.", "usb_", "use-", "use.", "use_", "used", "useful", "user", "using", "util", "utilitie", "utility", "vagrant", "validator", "value", "variou", "varnish", "version", "via-", "via.", "via_", "video", "view", "viewer", "vim-", "vim.", "vim_", "vimrc", "virtual", "vision", "visual", "vpn", "want", "warning", "watch", "watcher", "wave", "way-", "way.", "way_", "weather", "web-", "web_", "webapp", "webgl", "webhook", "webkit", "webrtc", "website", "websocket", "welcome", "welcome", "what", "what'", "when", "where", "which", "why-", "why.", "why_", "widget", "wifi", "wiki", "win-", "win.", "win_", "window", "wip-", "wip.", "wip_", "within", "without", "wizard", "word", "wordpres", "work", "worker", "workflow", "working", "workshop", "world", "wrapper", "write", "writer", "writing", "written", "www-", "www.", "www_", "xamarin", "xcode", "xml-", "xml.", "xml_", "xmpp", "xxxxxx", "yahoo", "yaml", "yandex", "yeoman", "yet-", "yet.", "yet_", "yii-", "yii.", "yii_", "youtube", "yui-", "yui.", "yui_", "zend", "zero", "zip-", "zip.", "zip_", "zsh-", "zsh.", "zsh_"]
regexTarget = "secret"
regexes = ["^(?:d1b243b8380caf21692ac7cfdcdee446d74bb06d40c0194c256686eb5f347feb|0d2c2229ff177d8534d6830389b4274b4cfd1ad194c9613f8e75290315ea77ca)$"]
"""
CONTROL_IDS = ("github", "entropy", "appended", "pem")
SCAN_TIMEOUT = 120
GIT_TIMEOUT = 60
MAX_SUMMARY_ROWS = 25
FINDING_EXIT_HISTORY = 1
FINDING_EXIT_CONTROL = 7
EXIT_OK = 0
EXIT_REJECT = 1
EXIT_USAGE = 2
DEFAULT_BASE = "refs/remotes/origin/main"
UNKNOWN_RULE = "unknown"
USAGE = ("usage: gate.py {history|controls|gate} "
         "--gitleaks <absolute-executable> --repo <absolute-root> "
         "[--base <revision>] [--candidate <revision>] [--config <absolute-file>]")

_RULE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class GateFailure(Exception):
    """Internal failure; public diagnostics must not expose its raw text."""


def write_default_config(destination: Path) -> None:
    """Write all default rules plus narrow digest exceptions (constant bytes)."""
    try:
        destination.write_text(CONFIG_TEXT, encoding="ascii")
    except OSError as exc:
        raise GateFailure("config write failed") from exc


def _token_suffix(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _github_shaped_token() -> str:
    # Split marker keeps this committed source inert under the scan.
    return "gh" + "p_" + _token_suffix(36)


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    length = len(text)
    return -sum(count / length * math.log2(count / length)
                for count in collections.Counter(text).values())


def _entropy_control() -> str:
    # Fixed mixed-case alphabet: above the default generic-api-key entropy
    # threshold, with no random stopword collisions or SHA-shaped allowlisting.
    # Construct at runtime so the committed source is inert under history scans.
    return "".join(chr(n) for n in range(65, 91)) + "".join(
        chr(n) for n in range(97, 123)) + "".join(chr(n) for n in range(48, 58))


def _pem_block() -> str:
    # Split markers keep this committed source inert under the scan.
    body = base64.b64encode(secrets.token_bytes(96)).decode("ascii")
    fence = "-----"
    lines = [fence + "BEGIN RS" + "A PRIVATE KEY" + fence]
    lines.extend(body[pos:pos + 64] for pos in range(0, len(body), 64))
    lines.append(fence + "END RS" + "A PRIVATE KEY" + fence)
    return "\n".join(lines) + "\n"


def generate_controls(destination: Path, context_file: Path) -> dict[str, Path]:
    """Create the four control plants under separate directories.

    Returns exactly CONTROL_IDS mapped to their directories. Raises
    GateFailure on missing/empty context or generation exhaustion.
    """
    if destination.is_symlink():
        raise GateFailure("scratch is a symlink")
    try:
        context_text = context_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateFailure("context unreadable") from exc
    if not context_text.strip():
        raise GateFailure("context empty")
    context_line = ""
    for line in context_text.splitlines():
        if GITLEAKS_SHA256 in line:
            context_line = line[:200]
            break
    if not context_line:
        raise GateFailure("context lacks checksum line")
    plants = {
        "github": 'token = "' + _github_shaped_token() + '"\n',
        "entropy": 'secret_key = "' + _entropy_control() + '"\n',
        "appended": context_line + " " + _github_shaped_token() + "\n",
        "pem": _pem_block(),
    }
    filenames = {"github": "creds.txt", "entropy": "settings.txt",
                 "appended": "appended.txt", "pem": "key.pem"}
    try:
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, 0o700)
    except OSError as exc:
        raise GateFailure("scratch setup failed") from exc
    result: dict[str, Path] = {}
    for control_id in CONTROL_IDS:
        control_dir = destination / control_id
        target = control_dir / filenames[control_id]
        try:
            control_dir.mkdir(mode=0o700, exist_ok=False)
            os.chmod(control_dir, 0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, plants[control_id].encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            raise GateFailure("plant write failed") from exc
        result[control_id] = control_dir
    return result


def summarize_findings(report: Path) -> list[tuple[str, int]]:
    """Count findings per sanitized rule id, sorted by count then rule.

    Raises GateFailure on missing/malformed reports. Never returns paths,
    matches or secret bytes.
    """
    try:
        raw = report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateFailure("report unreadable") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateFailure("report malformed") from exc
    if not isinstance(data, list):
        raise GateFailure("report wrong type")
    counts: dict[str, int] = {}
    for entry in data:
        if not isinstance(entry, dict):
            raise GateFailure("report entry wrong type")
        rule = entry.get("RuleID")
        if not isinstance(rule, str):
            raise GateFailure("report RuleID wrong type")
        clean = rule if _RULE_RE.fullmatch(rule) else UNKNOWN_RULE
        counts[clean] = counts.get(clean, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _print_counts(findings: list[tuple[str, int]]) -> None:
    for rule, count in findings[:MAX_SUMMARY_ROWS]:
        print(str(count) + " " + rule)


def _run_scanner(argv: list[str], report: Path, *, cwd: Path | None = None) -> int:
    """Run the scanner privately; stale reports never survive a call."""
    try:
        report.unlink(missing_ok=True)
    except OSError as exc:
        raise GateFailure("report cleanup failed") from exc
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
            timeout=SCAN_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise GateFailure("scanner timeout") from exc
    except OSError as exc:
        raise GateFailure("scanner launch failed") from exc
    return proc.returncode


def _require_tool(gitleaks: Path) -> None:
    if not gitleaks.is_absolute():
        raise GateFailure("tool path not absolute")
    if not gitleaks.is_file():
        raise GateFailure("tool not a file")
    if not os.access(gitleaks, os.X_OK):
        raise GateFailure("tool not executable")


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=GIT_TIMEOUT,
            check=False, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateFailure("git probe failed") from exc
    if proc.returncode != 0:
        raise GateFailure("git probe rejected")
    return proc.stdout.strip()


def _resolve_commit(repo: Path, revision: str) -> str:
    if not revision:
        raise GateFailure("empty revision")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}")


def _require_history_ready(repo: Path, candidate: str = "HEAD") -> str:
    if not _git(repo, "rev-parse", "--show-toplevel"):
        raise GateFailure("not a git repository")
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise GateFailure("shallow history")
    return _resolve_commit(repo, candidate)


def _snapshot_tree(repo: Path, candidate: str, destination: Path) -> None:
    """Extract raw candidate blobs without checkout filters or export attributes."""
    git = ["git", "--no-replace-objects", "-C", str(repo)]
    listing = subprocess.run(
        [*git, "ls-tree", "-r", "-z", candidate],
        capture_output=True, check=True, timeout=GIT_TIMEOUT).stdout
    entries = []
    for entry in listing.split(b"\0")[:-1]:
        metadata, name = entry.split(b"\t", 1)
        _, kind, oid = metadata.split()
        if kind == b"commit":  # Gitlinks carry an object ID, with no local file.
            continue
        parts = os.fsdecode(name).split("/")
        if kind != b"blob" or any(part in ("", ".", "..") for part in parts):
            raise GateFailure("invalid candidate tree")
        entries.append((oid, destination.joinpath(*parts)))

    # Spool the batch to disk so large tracked files don't require large buffers.
    with (tempfile.TemporaryFile(dir=destination.parent) as request,
          tempfile.TemporaryFile(dir=destination.parent) as blobs):
        request.writelines(oid + b"\n" for oid, _ in entries)
        request.seek(0)
        subprocess.run([*git, "cat-file", "--batch"], stdin=request, stdout=blobs,
                       stderr=subprocess.PIPE, check=True, timeout=GIT_TIMEOUT)
        blobs.seek(0)
        for oid, path in entries:
            header = blobs.readline().split()
            if len(header) != 3 or header[:2] != [oid, b"blob"]:
                raise GateFailure("candidate blob missing")
            remaining = int(header[2])
            path.parent.mkdir(parents=True, exist_ok=True)
            # Treat symlink payloads as files so the scan stays within the snapshot.
            with path.open("xb") as output:
                while remaining:
                    chunk = blobs.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise GateFailure("candidate blob truncated")
                    output.write(chunk)
                    remaining -= len(chunk)
            if blobs.read(1) != b"\n":
                raise GateFailure("candidate blob boundary invalid")


def _scan_release_leg(gitleaks: Path, config: Path, report: Path, label: str,
                      args: list[str], *, cwd: Path | None = None) -> None:
    status = _run_scanner(
        [str(gitleaks), "detect", *args, "--config", str(config),
         "--redact", "--no-banner", "--exit-code", str(FINDING_EXIT_HISTORY),
         "--report-format", "json", "--report-path", str(report)], report, cwd=cwd)
    findings = summarize_findings(report)
    if findings:
        _print_counts(findings)
    if status != 0 or findings:
        raise GateFailure(label + " rejected")
    print(label + ": clean")


def run_history(gitleaks: Path, repo: Path, scratch: Path, *,
                base: str | None = None, candidate: str = "HEAD",
                config: Path | None = None) -> None:
    """Require both the release history and the candidate tree to scan clean."""
    _require_tool(gitleaks)
    candidate_id = _require_history_ready(repo, candidate)
    revisions = candidate_id
    if base != "":
        if base is None:
            base = _git(repo, "show-ref", "--verify", "--hash", DEFAULT_BASE)
        base_id = _resolve_commit(repo, base)
        _git(repo, "merge-base", "--is-ancestor", base_id, candidate_id)
        revisions = f"{base_id}..{candidate_id}"
    if config is None:
        config = scratch / "gitleaks-history.toml"
        write_default_config(config)
    elif not config.is_absolute() or not config.is_file():
        raise GateFailure("invalid release config")

    # Include changes against each parent of a merge.
    _scan_release_leg(gitleaks, config, scratch / "history.json", "history",
                      ["--source", str(repo), "--log-opts=--diff-merges=separate --root " + revisions])
    with tempfile.TemporaryDirectory(prefix="candidate-tree-", dir=scratch) as directory:
        tree = Path(directory)
        _snapshot_tree(repo, candidate_id, tree)
        _scan_release_leg(gitleaks, config, scratch / "tree.json", "tree",
                          ["--no-git", "--source", "."], cwd=tree)


def run_controls(gitleaks: Path, repo: Path, scratch: Path) -> None:
    """Reject unless every runtime plant is independently detected."""
    _require_tool(gitleaks)
    context = repo / ".github" / "workflows" / "secrets.yml"
    if context.is_symlink():
        raise GateFailure("context is a symlink")
    generated = generate_controls(scratch / "controls", context)
    if set(generated) != set(CONTROL_IDS):
        raise GateFailure("control set mismatch")
    config = scratch / "gitleaks-controls.toml"
    write_default_config(config)
    failures = 0
    for control_id in CONTROL_IDS:
        report = scratch / ("control-" + control_id + ".json")
        try:
            status = _run_scanner(
                [str(gitleaks), "detect", "--no-git",
                 "--source", str(generated[control_id]),
                 "--config", str(config), "--redact", "--no-banner",
                 "--exit-code", str(FINDING_EXIT_CONTROL),
                 "--report-format", "json", "--report-path", str(report)],
                report)
            total = sum(count for _, count in summarize_findings(report))
        except GateFailure:
            status, total = -1, 0
        if status == FINDING_EXIT_CONTROL and total >= 1:
            print("control " + control_id + ": detected ("
                  + str(total) + " finding(s))")
        else:
            print("control " + control_id + ": NOT detected")
            failures += 1
    if failures:
        raise GateFailure("controls rejected")
    print("controls: clean")


def _parse(items: list[str]) -> tuple[str, Path, Path, str | None, str, Path | None]:
    if len(items) < 6 or len(items) % 2:
        raise GateFailure("arity")
    command = items[1]
    if command not in ("history", "controls", "gate"):
        raise GateFailure("command")
    flags = items[2::2]
    options = dict(zip(flags, items[3::2]))
    required = {"--gitleaks", "--repo"}
    allowed = required if command == "controls" else required | {"--base", "--candidate", "--config"}
    if len(options) != len(flags) or not required <= options.keys() <= allowed:
        raise GateFailure("options")
    gitleaks = Path(options["--gitleaks"])
    repo = Path(options["--repo"])
    config = Path(options["--config"]) if "--config" in options else None
    if (not gitleaks.is_absolute() or (not repo.is_absolute() and repo != Path("."))
            or (config is not None and not config.is_absolute())):
        raise GateFailure("relative path")
    return (command, gitleaks, repo.resolve(), options.get("--base"),
            options.get("--candidate", "HEAD"), config)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: 0 clean, 1 rejected, 2 malformed invocation."""
    items = list(sys.argv if argv is None else argv)
    if len(items) == 2 and items[1] in ("-h", "--help"):
        print(USAGE)
        return EXIT_OK
    try:
        command, gitleaks, repo, base, candidate, config = _parse(items)
    except GateFailure:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    try:
        scratch = Path(tempfile.mkdtemp(prefix="mv-secrets-"))
    except OSError:
        print(command + ": internal error")
        return EXIT_REJECT
    try:
        os.chmod(scratch, 0o700)
        if command == "history":
            run_history(gitleaks, repo, scratch, base=base, candidate=candidate, config=config)
        elif command == "controls":
            run_controls(gitleaks, repo, scratch)
        else:
            history_failed = False
            try:
                run_history(gitleaks, repo, scratch, base=base, candidate=candidate, config=config)
            except GateFailure:
                history_failed = True
            controls_failed = False
            try:
                run_controls(gitleaks, repo, scratch)
            except GateFailure:
                controls_failed = True
            if history_failed or controls_failed:
                raise GateFailure("gate rejected")
            print("gate: clean")
    except GateFailure:
        print(command + ": rejected")
        return EXIT_REJECT
    except Exception:
        # Fail closed without repr: tracebacks may carry paths or values.
        print(command + ": internal error")
        return EXIT_REJECT
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

