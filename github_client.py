import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from build_snapshot import decode, require


REPOSITORY = re.compile(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+")
TOOLS = r"(?:github copilot|copilot|cursor(?:agent)?|claude(?: code)?|codex|devin|aider|jules|sweep|gemini(?:-code-assist| code assist)?)"
TRAILER = re.compile(rf"^(?:co-authored|authored|assisted)-by:[ \t]*(?:{TOOLS}\b|[^\r\n<>]*<cursoragent@cursor\.com>)", re.I | re.M)
GENERATOR = re.compile(rf"^(?:🤖[ \t]*)?generated with[ \t]+\[?{TOOLS}\b", re.I | re.M)
DECLARATION = re.compile(r"\b(?:this (?:project|repository|codebase) (?:is|was|has been) (?:largely |mostly |primarily )?(?:ai-assisted|vibe[- ]coded|developed with ai|implemented with ai))\b", re.I)
TOPICS = {"ai-assisted", "vibe-coded", "ai-generated", "built-with-ai"}
CONFIG = (
    ".cursorrules", ".cursor/rules", ".aider.conf.yml", ".github/copilot-instructions.md",
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", ".windsurfrules", ".clinerules", ".github/instructions",
)


class GitHubError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def repository_name(url):
    if not url:
        return None
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    if parsed.scheme not in {"https", "http"} or parsed.hostname != "github.com" or parsed.username is not None:
        return None
    name = parsed.path.removeprefix("/").removesuffix("/").removesuffix(".git")
    return name.lower() if REPOSITORY.fullmatch(name) else None


def evidence(kind, weight, detail, url):
    return {"type": kind, "weight": weight, "detail": detail, "url": url}


def development_signals(repo, configs, topics, readme, commits):
    root = f"https://github.com/{repo}"
    signals = []
    for kind, pattern, description in (
        ("commit_trailer", TRAILER, "AI coding tool named in a commit trailer"),
        ("generator_footer", GENERATOR, "AI coding tool named in a commit generator footer"),
    ):
        for item in commits:
            if pattern.search(item["commit"]["message"]):
                sha = item["sha"]
                require(re.fullmatch(r"[a-f0-9]{40}", sha), "invalid GitHub commit SHA")
                signals.append(evidence(kind, 1.0, description, f"{root}/commit/{sha}"))
                break
    if configs:
        path = configs[0]
        signals.append(evidence("tool_config", 0.6, path, f"{root}/blob/HEAD/{path}"))
    if matched := sorted(TOPICS.intersection(topics)):
        signals.append(evidence("repository_topic", 0.6, matched[0], root))
    if readme and DECLARATION.search(readme):
        signals.append(evidence("readme_declaration", 0.3, "README explicitly describes AI-assisted development", f"{root}#readme"))
    return signals


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())
        self.budgets = {}

    def request(self, path, query=None):
        budget = "graphql" if query is not None else "rest"
        remaining, reset = self.budgets.get(budget, (200, None))
        if remaining < 200:
            raise GitHubError(f"GitHub {budget} remaining budget is {remaining}; resume after {reset or 'the rate limit resets'}")
        headers = {
            "Authorization": f"Bearer {self.token}", "User-Agent": "hhf-app-catalogue-data",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
        }
        body = json.dumps({"query": query}).encode() if query is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(f"https://api.github.com{path}", data=body, headers=headers)
        for attempt in range(5):
            try:
                with self.opener.open(req, timeout=30) as response:
                    if budget == "rest" and "X-RateLimit-Remaining" in response.headers:
                        self.budgets[budget] = (int(response.headers["X-RateLimit-Remaining"]), response.headers.get("X-RateLimit-Reset"))
                    payload = response.read(20_000_001)
                    require(len(payload) <= 20_000_000, "oversized GitHub response")
                    return decode(payload)
            except HTTPError as error:
                # 5xx is GitHub answering under load, not a rejection: the request is a
                # read, so a short bounded backoff keeps a 1,200-repository run alive
                # instead of losing the whole snapshot to one upstream hiccup.
                transient = error.code in (500, 502, 503, 504)
                if not transient and error.code not in (403, 429):
                    raise GitHubError(f"GitHub API returned HTTP {error.code}") from None
                if transient:
                    wait = 2**attempt
                else:
                    retry_after = error.headers.get("Retry-After")
                    if error.headers.get("X-RateLimit-Remaining") == "0":
                        reset = error.headers.get("X-RateLimit-Reset", "the next quota window")
                        raise GitHubError(f"GitHub exhausted its quota; resume after {reset}") from None
                    wait = max(int(retry_after or 0), 60 * 2**attempt)
                if attempt == 4 or wait > 120:
                    raise GitHubError(f"GitHub returned HTTP {error.code}; the run can resume from its checkpoint") from None
                time.sleep(wait)
            except (URLError, TimeoutError) as error:
                # A dropped connection or a read timeout behaves like a 5xx here.
                if attempt == 4:
                    raise GitHubError(f"GitHub request failed ({type(error).__name__}); the run can resume from its checkpoint") from None
                time.sleep(2**attempt)

    def repositories(self, repos, fields):
        aliases = []
        for index, name in enumerate(repos):
            owner, repo = (json.dumps(part) for part in name.split("/"))
            aliases.append(f"r{index}:repository(owner:{owner},name:{repo}){{isPrivate nameWithOwner {fields}}}")
        result = self.request("/graphql", "{" + " ".join(aliases) + " rateLimit{remaining resetAt}}")
        missing = set()
        for error in result.get("errors", []):
            path = error.get("path", [])
            if error.get("type") != "NOT_FOUND" or len(path) != 1 or not isinstance(path[0], str) or not re.fullmatch(r"r\d+", path[0]):
                raise GitHubError("GitHub GraphQL query did not complete")
            missing.add(path[0])
        data = result["data"]
        rate = data["rateLimit"]
        self.budgets["graphql"] = (rate["remaining"], rate["resetAt"])
        repos_by_name = {}
        for index, repo in enumerate(repos):
            alias = f"r{index}"
            row = data[alias]
            if alias in missing or row is None or row["isPrivate"] is not False:
                repos_by_name[repo] = None
            else:
                require(REPOSITORY.fullmatch(row["nameWithOwner"]), "invalid canonical GitHub repository")
                repos_by_name[repo] = row
        return repos_by_name

    def metrics(self, repos):
        return self.repositories(repos, """
            stargazerCount forkCount createdAt isArchived licenseInfo{spdxId pseudoLicense} primaryLanguage{name}
            defaultBranchRef{target{... on Commit{committedDate}}}
        """)

    def ai_configs(self, repos):
        paths = " ".join(f"p{i}:object(expression:{json.dumps('HEAD:' + path)}){{__typename}}" for i, path in enumerate(CONFIG))
        return self.repositories(repos, paths + " readme:object(expression:\"HEAD:README.md\"){... on Blob{text isTruncated}} repositoryTopics(first:30){nodes{topic{name}}}")

    def ai(self, repo, config, has_commits):
        commits = []
        if has_commits:
            for page in (1, 2):
                result = self.request(f"/repos/{quote(repo, safe='/')}/commits?per_page=100&page={page}")
                require(isinstance(result, list), "GitHub commits response must be a list")
                commits.extend(result)
                if len(result) < 100:
                    break
        paths = [path for i, path in enumerate(CONFIG) if config[f"p{i}"] is not None]
        readme = config.get("readme") or {}
        text = readme.get("text") if not readme.get("isTruncated") else None
        topics = [node["topic"]["name"] for node in config["repositoryTopics"]["nodes"]]
        return development_signals(repo, paths, topics, text, commits)
