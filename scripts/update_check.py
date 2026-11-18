"""检测 iwp 技能上游是否有更新（维护面能力，见 SKILL.md 第 0 步）。

定位：维护面能力，不参与业务链路。纯标准库、尽力而为——任何异常都不向
调用方抛出，不改变 preflight 的 ok 判定与退出码；每天最多发起一次匿名
GitHub 请求，失败后按退避窗口重试。

判定基准（方案 §3.2 / §3.3：三形态探测，见 docs 存档）
    形态 git      目录含 .git 且 origin 归一后等于目标仓库
         npx      `npx --no-install skills list -g --json` 中存在 path 等于本目录、
                  sourceUrl 等于目标仓库、status 为 tracked 的记录
         bare     以上都不成立（解压安装、手工拷贝，两者无需区分）
         git_foreign 含 .git 但 origin 不等于目标仓库（分叉/开发副本），不自动更新

    本地版本标识按形态取：
         git   `git rev-parse HEAD`
         npx   锁记录的 skillFolderHash
         bare  没有提交身份，只能逐文件对账（发布集指纹 PSF 的唯一使用方）

    状态语义：
         git   HEAD 与远端 sha 比；工作树脏 -> drift，双变 -> conflict
         npx   锁 sha 与远端 sha 比；本地改动无法判定，只产出 synced/behind
         bare  发布集逐路径对账，四态齐全
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

# skill_config 位于技能根目录（scripts/ 的上一级）；直接运行本文件时
# sys.path[0] 是 scripts/，需显式补根目录才能 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import skill_config  # noqa: E402

# === 路径与身份 ===
SKILL_DIR: Path = skill_config.SKILL_DIR
STATE_FILE: Path = SKILL_DIR / ".update_state.json"
LOCK_FILE: Path = Path.home() / ".agents" / ".skill-lock.json"
GITIGNORE_FILE: Path = SKILL_DIR / ".gitignore"
DEFAULT_REPO = "TKOHP/iwp-skill"

# === 安装形态 ===
FORM_GIT = "git"
FORM_NPX = "npx"
FORM_BARE = "bare"
FORM_FOREIGN = "git_foreign"
FORMS = (FORM_GIT, FORM_NPX, FORM_BARE, FORM_FOREIGN)

# === 输入校验（拒绝拼进 URL / 命令的任意值）===
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")

# === 时间预算（方案 §8 效率模型）===
CHECK_INTERVAL_S = 24 * 3600    # 两次真实探测的最小间隔
GATE_INTERVAL_S = 24 * 3600     # 同一 (远端 sha, 状态) 的重复提示间隔
MAX_CLOCK_SKEW_S = 3600         # 状态时间戳超前该值视为时钟异常，重新探测
FAIL_RETRY_S = 900              # 探测失败后的重试间隔（网络抖动不该罚满一天）
RATE_LIMIT_RETRY_S = 3600       # 匿名配额耗尽（403/429）时的兜底重试间隔

# === 状态文件契约 ===
STATE_SCHEMA = 3
STATE_KEYS = (
    "schema", "repo", "last_check_at", "last_result", "last_error",
    "remote_sha", "remote_date", "snapshot",
    "last_notified_key", "last_notified_at", "retry_at",
)

# === 发布集上限（方案 §4.7 R12）===
MAX_MANIFEST_PATHS = 2000        # 超过只记 psf，不再记逐路径清单
MAX_MANIFEST_BYTES = 512 * 1024  # 清单 JSON 体积上限
MAX_FILE_BYTES = 4 * 1024 * 1024  # 单文件超过即视为运行产物，不进发布集
MAX_DIRTY_PATHS = 200            # git 形态工作树脏文件列表上限

_OFF_VALUES = {"", "0", "false", "no", "off"}
_STATES = ("synced", "behind", "drift", "conflict", "unknown")

# npx 归属探测：--no-install 保证只读本地缓存、不触发下载。
# 该命令为固定常量，不接受外部拼接，故使用 shell 解析 PATHEXT（npx.cmd）。
NPX_LIST_CMD = "npx --no-install skills list -g --json"
NPX_TIMEOUT_S = 20.0


def enabled() -> bool:
    """IWP_UPDATE_CHECK：默认开；0/false/no/off 关闭整项能力（检测 + 执行）。

    本函数是更新能力的唯一总开关：preflight 附带检测、独立诊断、以及
    scripts/skill_update.py 的执行入口（含预览）都以它为准。
    """
    raw = skill_config._cfg("IWP_UPDATE_CHECK", "1").strip().lower()
    return raw not in _OFF_VALUES


def repo_slug() -> str:
    """IWP_UPDATE_REPO：owner/repo；空值回退默认仓库。"""
    raw = skill_config._cfg("IWP_UPDATE_REPO", DEFAULT_REPO).strip()
    return raw or DEFAULT_REPO


def _now() -> float:
    import time

    return time.time()


def _state_path(path: Path | None) -> Path:
    return STATE_FILE if path is None else path


# === 状态读写（损坏/未知 schema/repo 不符一律当不存在，下次自愈）===

def read_state(path: Path | None = None) -> dict[str, Any]:
    p = _state_path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        return {}
    if raw.get("repo") != repo_slug():
        return {}
    return raw


def write_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """原子写状态；失败返回 False（调用方据此省略输出块）。"""
    p = _state_path(path)
    payload = {key: state.get(key) for key in STATE_KEYS}
    payload["schema"] = STATE_SCHEMA
    payload["repo"] = repo_slug()
    tmp: str | None = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".update_state.", suffix=".tmp", dir=str(p.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def state_writable(path: Path | None = None) -> bool:
    """网络请求前的廉价可写性预检（mkstemp 探测后立即删除）。"""
    p = _state_path(path)
    tmp: str | None = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".update_state.probe.", dir=str(p.parent))
        os.close(fd)
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def check_due(now: float, state: dict[str, Any],
              interval: float = CHECK_INTERVAL_S) -> bool:
    """门禁：上次尝试在窗内则跳过；时间戳缺失/超前则重新探测。

    失败路径另写 retry_at：只等 FAIL_RETRY_S 而非整天，避免一次网络抖动
    让检测静默一天。
    """
    retry = state.get("retry_at")
    if isinstance(retry, (int, float)) and not isinstance(retry, bool):
        if retry > now + MAX_CLOCK_SKEW_S:
            return True
        if now < retry:
            return False
        return True
    last = state.get("last_check_at")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return True
    if last > now + MAX_CLOCK_SKEW_S:
        return True
    return now - last >= interval

# === 判定内核（纯函数：无 IO、无网络、无时钟副作用）===

def valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def valid_repo(value: Any) -> bool:
    if not isinstance(value, str) or ".." in value:
        return False
    return bool(_REPO_RE.fullmatch(value))


def blob_sha(data: bytes) -> str:
    """git blob 对象标识：sha1("blob <字节数>\\0" + 内容)。"""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def psf(manifest: dict[str, str]) -> str:
    """发布集指纹：按路径 UTF-8 字节序，逐项写入 len(path)||path||len(blob)||blob。"""
    digest = hashlib.sha256()
    for path in sorted(manifest, key=lambda p: p.encode("utf-8")):
        raw_path = path.encode("utf-8")
        raw_blob = manifest[path].encode("utf-8")
        digest.update(len(raw_path).to_bytes(4, "big"))
        digest.update(raw_path)
        digest.update(len(raw_blob).to_bytes(4, "big"))
        digest.update(raw_blob)
    return digest.hexdigest()


def parse_remote(payload: Any) -> dict[str, str] | None:
    """GitHub `commits/main` 响应 -> {"sha", "date"}；形状不符返回 None。"""
    if not isinstance(payload, dict):
        return None
    sha = payload.get("sha")
    if not valid_sha(sha):
        return None
    date = ""
    commit = payload.get("commit")
    if isinstance(commit, dict):
        for key in ("committer", "author"):
            who = commit.get(key)
            if isinstance(who, dict) and isinstance(who.get("date"), str):
                date = who["date"][:10]
                break
    return {"sha": sha, "date": date}


def parse_tree(payload: Any) -> dict[str, Any] | None:
    """Tree API（recursive）响应 -> {"manifest", "psf", "paths"}。

    只收普通文件 blob：跳过目录、符号链接（120000）、子模块（160000）
    与超过单文件上限的条目；`truncated != false` 一律视为失败（清单不完整）。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("truncated") is not False:
        return None
    entries = payload.get("tree")
    if not isinstance(entries, list):
        return None
    manifest: dict[str, str] = {}
    excluded = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "blob":
            continue
        if entry.get("mode") not in ("100644", "100755"):
            continue
        path = entry.get("path")
        sha = entry.get("sha")
        if not isinstance(path, str) or not path or not valid_sha(sha):
            continue
        size = entry.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size > MAX_FILE_BYTES:
            excluded += 1
            continue
        manifest[path] = sha
    if not manifest:
        return None
    return {"manifest": manifest, "psf": psf(manifest),
            "paths": len(manifest), "excluded": excluded}


def classify(remote: dict[str, str], local: dict[str, str] | None,
             recorded: Any) -> dict[str, Any] | None:
    """逐路径比对 -> {"status", "detail"}；本地读不到返回 None（无证据）。

    status：
        synced   全部路径与远端一致，且无「上游已删、本地仍留旧版」
        behind   本地是旧版（上游新增/修改/删除，本地未动）
        drift    只有本地被改（上游等于上次记录）
        conflict 双方都改
        unknown  没有上次记录，只能判「不一致」
    """
    if local is None:
        return None
    previous = recorded if isinstance(recorded, dict) else None
    detail: dict[str, list[str]] = {"modified": [], "missing": [], "stale": [], "extra": []}
    behind = drift = conflict = False
    for path, sha in remote.items():
        have = local.get(path)
        if have is None:
            detail["missing"].append(path)
            behind = True
            continue
        if have == sha:
            continue
        detail["modified"].append(path)
        was = previous.get(path) if previous else None
        if was is None or have == was:
            behind = True
        elif sha == was:
            drift = True
        else:
            conflict = True
    if previous:
        for path, was in previous.items():
            if path in remote:
                continue
            have = local.get(path)
            if have is not None and have == was:
                detail["stale"].append(path)
                behind = True
    detail["extra"] = sorted(p for p in local if p not in remote)
    if conflict:
        status = "conflict"
    elif behind:
        status = "behind" if previous is not None else "unknown"
    elif drift:
        status = "drift"
    else:
        status = "synced"
    return {"status": status, "detail": detail}


def compare_snapshot(local: dict[str, str] | None,
                     expected: dict[str, str]) -> dict[str, Any] | None:
    """同源快照比对（远端未变时的快捷判定）：任意差异都只能来自本地改动。

    local 读不到返回 None；一致 -> synced，否则 -> drift。
    """
    if local is None:
        return None
    detail: dict[str, list[str]] = {"modified": [], "missing": [], "stale": [], "extra": []}
    for path, sha in expected.items():
        have = local.get(path)
        if have is None:
            detail["missing"].append(path)
        elif have != sha:
            detail["modified"].append(path)
    detail["extra"] = sorted(p for p in local if p not in expected)
    changed = any(detail[kind] for kind in ("modified", "missing", "extra"))
    return {"status": "drift" if changed else "synced", "detail": detail}


def storable_manifest(manifest: dict[str, str]) -> dict[str, str] | None:
    """超上限只记 psf（方案 §4.7 R12）；返回 None 表示不写逐路径清单。"""
    if len(manifest) > MAX_MANIFEST_PATHS:
        return None
    try:
        size = len(json.dumps(manifest, ensure_ascii=True, separators=(",", ":")))
    except (TypeError, ValueError):
        return None
    return manifest if size <= MAX_MANIFEST_BYTES else None


def prompt_allowed(now: float, state: dict[str, Any], key: str) -> bool:
    """提示闸门：同一 (远端 sha, 状态) 24h 内不重复提示；变化可立即再提示。"""
    if not key:
        return False
    if state.get("last_notified_key") != key:
        return True
    at = state.get("last_notified_at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True
    if at > now + MAX_CLOCK_SKEW_S:
        return True
    return now - at >= GATE_INTERVAL_S


def action_for(form: str) -> str:
    """给 agent 的动作标签：执行路径由执行器按形态自行分派，统一为 update。"""
    return "update"


def update_block(form: str, remote: dict[str, Any], status: str,
                 available: bool, prompt: bool,
                 detail: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """preflight 顶层 update 块：只描述事实与是否提示，不含路径清单。"""
    block = {
        "checked": True,
        "available": available,
        "form": form,
        "status": status,
        "remote_sha": remote["sha"],
        "remote_date": remote["date"],
        "action": action_for(form),
        "prompt": prompt,
    }
    if detail is not None:
        block["detail"] = {kind: len(paths) for kind, paths in detail.items()}
    return block

# === 远端探测（匿名、硬预算）===
API_COMMIT_FMT = "https://api.github.com/repos/{repo}/commits/main"
API_TREE_FMT = "https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
HTTP_TIMEOUT_S = 15.0       # urllib 单次请求超时（实测 1.9–9.7s，跨境链路波动极大）
PROBE_BUDGET_S = 30.0       # 单次远端探测的硬截止，超出即静默放弃
MAX_RESPONSE_BYTES = 256 * 1024
TREE_MAX_BYTES = 1024 * 1024
_USER_AGENT = "iwp-skill-update-check/3"


class RateLimited(Exception):
    """匿名配额耗尽（403/429）：带上建议重试时刻，供调用方退避。"""

    def __init__(self, retry_at: float) -> None:
        super().__init__("rate limited")
        self.retry_at = retry_at


def _retry_at_from_headers(headers: Any, now: float) -> float:
    """优先 x-ratelimit-reset（epoch 秒），其次 Retry-After（秒），兜底 1h。"""
    reset = headers.get("x-ratelimit-reset") if headers is not None else None
    try:
        value = float(reset)
        if value > now:
            return value
    except (TypeError, ValueError):
        pass
    after = headers.get("retry-after") if headers is not None else None
    try:
        return now + max(60.0, float(after))
    except (TypeError, ValueError):
        return now + RATE_LIMIT_RETRY_S


def _http_get_json(url: str, max_bytes: int = MAX_RESPONSE_BYTES,
                   timeout: float = HTTP_TIMEOUT_S) -> Any:
    """GET 并解析 JSON；配额耗尽转成 RateLimited，其余异常由调用方吞掉。"""
    import time
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise RateLimited(
                _retry_at_from_headers(exc.headers, time.time())
            ) from exc
        raise
    if len(raw) > max_bytes:
        raise ValueError(f"response too large: >{max_bytes} bytes")
    return json.loads(raw.decode("utf-8"))


def _fetch_with_retry(fetch: Any, url: str, max_bytes: int | None = None,
                      attempts: int = 2) -> Any:
    """同一 URL 最多试 attempts 次：TLS 握手/读超时属抖动，重试一次即可覆盖；
    配额耗尽（RateLimited）不重试，直接上抛给调用方退避。"""
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return fetch(url) if max_bytes is None else fetch(url, max_bytes)
        except RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常一律重试
            last = exc
    raise last if last is not None else RuntimeError("fetch failed")


def _run_with_budget(work: Any, budget_s: float,
                     hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """在守护线程里跑远端探测，超预算即放弃；返回结果字典或 None。"""
    import threading

    box: dict[str, Any] = {}

    def _wrapped() -> None:
        try:
            box["ok"] = work()
        except RateLimited as exc:
            box["failed"] = True
            box["retry_at"] = exc.retry_at
        except Exception:  # noqa: BLE001 - 探测失败一律静默
            box["failed"] = True

    worker = threading.Thread(target=_wrapped, name="iwp-update-probe", daemon=True)
    worker.start()
    worker.join(budget_s)
    if hint is not None and isinstance(box.get("retry_at"), (int, float)):
        hint["retry_at"] = float(box["retry_at"])
    if worker.is_alive() or box.get("failed"):
        return None
    return box.get("ok")


def fetch_commit(repo: str, *, budget_s: float = PROBE_BUDGET_S,
                 fetch: Any = None,
                 hint: dict[str, Any] | None = None) -> dict[str, str] | None:
    """取远端 main 的 commit 身份；失败/超预算返回 None（静默）。"""
    if not valid_repo(repo):
        return None
    fetch = fetch or _http_get_json

    def _work() -> dict[str, str] | None:
        return parse_remote(_fetch_with_retry(fetch, API_COMMIT_FMT.format(repo=repo)))

    return _run_with_budget(_work, budget_s, hint)


def fetch_tree(repo: str, sha: str, *, budget_s: float = PROBE_BUDGET_S,
               fetch: Any = None,
               hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """取远端发布集（仅 bare 形态需要）；失败/超预算返回 None（静默）。"""
    if not valid_repo(repo) or not valid_sha(sha):
        return None
    fetch = fetch or _http_get_json

    def _work() -> dict[str, Any] | None:
        return parse_tree(_fetch_with_retry(
            fetch, API_TREE_FMT.format(repo=repo, sha=sha), TREE_MAX_BYTES,
        ))

    return _run_with_budget(_work, budget_s, hint)


def fetch_remote_manifest(repo: str, *, budget_s: float = PROBE_BUDGET_S,
                          fetch: Any = None,
                          hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """commit + tree 一次取全（执行器需要发布集时使用）。"""
    if not valid_repo(repo):
        return None
    fetch = fetch or _http_get_json

    def _work() -> dict[str, Any] | None:
        commit = parse_remote(
            _fetch_with_retry(fetch, API_COMMIT_FMT.format(repo=repo))
        )
        if commit is None:
            return None
        tree = parse_tree(_fetch_with_retry(
            fetch, API_TREE_FMT.format(repo=repo, sha=commit["sha"]),
            TREE_MAX_BYTES,
        ))
        if tree is None:
            return None
        return {**commit, **tree}

    return _run_with_budget(_work, budget_s, hint)

# === 本地发布集（原始字节复算）===
GIT_TIMEOUT_S = 2.0


def _run_git(args: list[str], *, cwd: Path, timeout: float = GIT_TIMEOUT_S,
             stdin_data: str | None = None) -> tuple[int, str]:
    """执行 git；不可用/超时统一返回 (-1, "")。"""
    import subprocess

    # stdin 必须以 UTF-8 字节传递：Windows 上默认 locale 编码（cp936）会让
    # 中文路径变成乱码，git 找不到文件直接 128 退出。
    data = stdin_data.encode("utf-8") if stdin_data is not None else None
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            timeout=timeout, check=False, input=data,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    # 只去行尾换行：porcelain 输出首行以「空格 + 状态位」开头，整体 strip()
    # 会吃掉那个空格，导致脏文件路径少一个字符
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip("\r\n")


def local_manifest(paths: list[str], *, skill_dir: Path | None = None,
                   expect: dict[str, str] | None = None) -> dict[str, str] | None:
    """普通目录：直接读字节复算 blob sha；任一文件不可读则整体无证据。

    Windows 检出的副本行尾为 CRLF，而仓库里存的是 LF；原始字节不等且
    期望值存在时，再按 CRLF->LF 归一复算一次（只影响文本，二进制无 CRLF
    不受影响），避免整包假差异。
    """
    d = SKILL_DIR if skill_dir is None else skill_dir
    out: dict[str, str] = {}
    for path in paths:
        target = d / path
        try:
            if not target.is_file():
                continue
            data = target.read_bytes()
        except OSError:
            return None
        digest = blob_sha(data)
        wanted = expect.get(path) if expect else None
        if wanted is not None and digest != wanted and b"\r\n" in data:
            digest = blob_sha(data.replace(b"\r\n", b"\n"))
        out[path] = digest
    return out


def parse_github_repo(url: Any) -> str | None:
    """把 https/ssh/git@ 形式的远端地址归一成 owner/repo；非 GitHub 或非法返回 None。"""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if text.startswith("git@"):
        _host, _, path = text[4:].partition(":")
        repo = path
    elif "://" in text:
        rest = text.split("://", 1)[1]
        host, _, path = rest.partition("/")
        if not path:
            return None
        host = host.rsplit("@", 1)[-1]
        if host.lower() not in ("github.com", "www.github.com"):
            return None
        repo = path
    else:
        repo = text
    repo = repo.strip().rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo if valid_repo(repo) else None


def normalize_path(value: Any) -> str | None:
    """Windows 路径归一：分隔符统一为 \\\\、去尾部、大小写不敏感。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("/", "\\")
    while len(text) > 3 and text.endswith("\\"):
        text = text[:-1]
    return text.casefold()


def _lock_entry(lock_path: Path | None = None) -> dict[str, Any] | None:
    """读锁文件中 iwp 条目；锁不存在/损坏/无该条目一律返回 None。"""
    p = LOCK_FILE if lock_path is None else lock_path
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    skills = data.get("skills")
    entry = skills.get("iwp") if isinstance(skills, dict) else None
    return entry if isinstance(entry, dict) else None


def git_dirty_paths(skill_dir: Path | None = None,
                    run: Any = None) -> list[str] | None:
    """工作树脏文件（忽略未跟踪文件，状态文件不计入改动）；读不到返回 None。"""
    d = SKILL_DIR if skill_dir is None else skill_dir
    runner = run or _run_git
    rc, out = runner(["status", "--porcelain", "--untracked-files=no"], cwd=d)
    if rc != 0:
        return None
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:            # 重命名：取新路径
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.append(path)
        if len(paths) >= MAX_DIRTY_PATHS:
            break
    return paths


def _list_npx_skills(timeout: float = NPX_TIMEOUT_S) -> list[dict[str, Any]] | None:
    """`skills list -g --json`（离线、只读）；Node 缺失或命令失败返回 None。"""
    import shutil
    import subprocess

    if shutil.which("npx") is None:
        return None
    try:
        proc = subprocess.run(
            NPX_LIST_CMD, shell=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def match_npx_entry(entries: list[dict[str, Any]], skill_dir: Path,
                    repo: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """在 list 结果里找「本目录」的记录，同时返回「同仓库的其他副本」。

    归属只认 path 相等，其次校验 sourceUrl；锁文件本身不作为归属证据。
    """
    target = normalize_path(str(skill_dir))
    hit: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []
    for entry in entries:
        path = entry.get("path")
        source = (parse_github_repo(entry.get("sourceUrl"))
                  or parse_github_repo(entry.get("source")))
        if normalize_path(path) == target:
            hit = {**entry, "_repo": source}
        elif source == repo:
            others.append({
                "path": path,
                "status": entry.get("status"),
                "scope": entry.get("scope"),
            })
    return hit, others


def detect_profile(skill_dir: Path | None = None, *, repo: str | None = None,
                   run: Any = None, list_skills: Any = None) -> dict[str, Any]:
    """判定安装形态，返回 {"form", "reason", "evidence"}。

    顺序固定：git -> git_foreign -> npx -> bare，命中即停；npx 必须有正面证据
    （path + sourceUrl + tracked），不以「没有 .git」反推「是 npx」。
    """
    d = SKILL_DIR if skill_dir is None else skill_dir
    target_repo = repo_slug() if repo is None else repo
    runner = run or _run_git
    evidence: dict[str, Any] = {"skill_dir": str(d), "repo": target_repo}

    try:
        has_git = (d / ".git").exists()
    except OSError:
        has_git = False

    if has_git:
        rc, origin = runner(["remote", "get-url", "origin"], cwd=d)
        parsed = parse_github_repo(origin) if rc == 0 else None
        evidence["origin"] = origin if rc == 0 else None
        if parsed == target_repo:
            return {"form": FORM_GIT, "reason": "git_origin_match", "evidence": evidence}
        return {
            "form": FORM_FOREIGN,
            "reason": "git_origin_mismatch" if parsed else "git_origin_unreadable",
            "evidence": evidence,
        }

    entries = (list_skills or _list_npx_skills)()
    if entries is None:
        evidence["npx_list"] = "unavailable"
        return {"form": FORM_BARE, "reason": "npx_list_failed", "evidence": evidence}

    hit, others = match_npx_entry(entries, d, target_repo)
    evidence["npx_listed"] = len(entries)
    if others:
        evidence["other_copies"] = others
    if hit is None:
        return {"form": FORM_BARE, "reason": "npx_no_path_match", "evidence": evidence}
    evidence["npx_status"] = hit.get("status")
    evidence["npx_source"] = hit.get("sourceUrl") or hit.get("source")
    if hit.get("_repo") != target_repo:
        return {"form": FORM_BARE, "reason": "npx_source_mismatch", "evidence": evidence}
    if hit.get("status") != "tracked":
        return {"form": FORM_BARE, "reason": "npx_lock_untracked", "evidence": evidence}
    return {"form": FORM_NPX, "reason": "npx_path_match", "evidence": evidence}


def local_commit_id(form: str, *, skill_dir: Path | None = None,
                    lock_path: Path | None = None, run: Any = None) -> str | None:
    """本地版本标识：git 取 HEAD，npx 取锁的 skillFolderHash，其余无。"""
    if form == FORM_GIT:
        d = SKILL_DIR if skill_dir is None else skill_dir
        rc, out = (run or _run_git)(["rev-parse", "HEAD"], cwd=d)
        return out if rc == 0 and valid_sha(out) else None
    if form == FORM_NPX:
        entry = _lock_entry(lock_path)
        value = entry.get("skillFolderHash") if entry else None
        return value.lower() if valid_sha(value) else None
    return None


def keepalive_names(gitignore: Path | None = None) -> list[str] | None:
    """从 .gitignore 派生保活集合（唯一来源，见方案 §3.6 / R8）。

    规则：取不含通配符（* ? [）且不以 / 结尾的条目——即「不进版本库、也不进
    发布包」的根级文件。读不到 .gitignore 返回 None（调用方据此拒绝覆盖式更新）。
    """
    p = GITIGNORE_FILE if gitignore is None else gitignore
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line.endswith("/"):
            continue
        if any(ch in line for ch in "*?["):
            continue
        if "/" in line or "\\" in line:
            continue
        if line not in out:
            out.append(line)
    return out


def _snapshot_for(state: dict[str, Any], sha: str) -> dict[str, Any] | None:
    """同源快照：sha 相同且含逐路径清单才可用于快捷判定（方案 R5）。"""
    snap = state.get("snapshot")
    if not isinstance(snap, dict):
        return None
    if not valid_sha(snap.get("sha")) or snap.get("sha") != sha:
        return None
    manifest = snap.get("manifest")
    if not isinstance(manifest, dict) or not manifest:
        return None
    if any(not valid_sha(v) for v in manifest.values()):
        return None
    return snap


# === 编排（唯一对外入口；永不抛异常）===

def maybe_check(*, now: float | None = None,
                write: bool = True) -> dict[str, Any] | None:
    """会话首次调用前运行：需要时返回 update 块，否则返回 None。"""
    try:
        return _maybe_check(now=now, write=write)
    except Exception:
        return None


def _probe_failed(state: dict[str, Any], moment: float, error: str,
                  retry_at: float | None = None) -> None:
    write_state({
        **state,
        "last_check_at": moment,
        "last_result": "error",
        "last_error": error,
        "retry_at": retry_at if retry_at is not None else moment + FAIL_RETRY_S,
    })


def _maybe_check(*, now: float | None, write: bool) -> dict[str, Any] | None:
    if not enabled():
        return None
    moment = _now() if now is None else now
    state = read_state()
    if not check_due(moment, state):
        return None
    if not state_writable():
        return None
    repo = repo_slug()
    profile = detect_profile(repo=repo)
    form = profile["form"]
    if form == FORM_FOREIGN:
        if write:
            _probe_failed(state, moment, profile["reason"])
        return None
    hint: dict[str, Any] = {}
    commit = fetch_commit(repo, hint=hint)
    if commit is None:
        if write:
            _probe_failed(state, moment, "probe_failed", hint.get("retry_at"))
        return None

    verdict: dict[str, Any] | None = None
    manifest: dict[str, str] | None = None
    psf_value: str | None = None
    dirty: list[str] | None = None

    if form == FORM_BARE:
        snapshot = _snapshot_for(state, commit["sha"])
        if snapshot is not None:
            manifest = snapshot["manifest"]
            psf_value = snapshot.get("psf") if isinstance(snapshot.get("psf"), str) else psf(manifest)
            verdict = compare_snapshot(local_manifest(list(manifest)), manifest)
        else:
            tree = fetch_tree(repo, commit["sha"], hint=hint)
            if tree is None:
                if write:
                    _probe_failed(state, moment, "probe_failed", hint.get("retry_at"))
                return None
            manifest = tree["manifest"]
            psf_value = tree["psf"]
            previous = state.get("snapshot")
            previous_manifest = previous.get("manifest") if isinstance(previous, dict) else None
            verdict = classify(manifest, local_manifest(list(manifest), expect=manifest),
                               previous_manifest)
        status = "no_evidence" if verdict is None else verdict["status"]
    else:
        local_id = local_commit_id(form)
        if local_id is None:
            status = "no_evidence"
        elif form == FORM_GIT:
            dirty = git_dirty_paths()
            if dirty is None:
                status = "no_evidence"
            elif local_id == commit["sha"]:
                status = "drift" if dirty else "synced"
            else:
                status = "conflict" if dirty else "behind"
            if dirty:
                verdict = {"status": status,
                           "detail": {"modified": dirty, "missing": [],
                                      "stale": [], "extra": []}}
        else:
            status = "synced" if local_id == commit["sha"] else "behind"

    available = status not in ("synced", "no_evidence")
    key = f"{commit['sha']}:{status}"
    prompt = available and prompt_allowed(moment, state, key)
    new_state: dict[str, Any] = {
        **state,
        "last_check_at": moment,
        "last_result": status,
        "last_error": None,
        "remote_sha": commit["sha"],
        "remote_date": commit["date"],
        "retry_at": None,
    }
    # 快照只在「bare 形态且本次一致」时写入，且 sha/发布集/psf 同时落盘（R5）
    if form == FORM_BARE and status == "synced" and manifest is not None:
        storable = storable_manifest(manifest)
        if storable is not None:
            new_state["snapshot"] = {
                "sha": commit["sha"],
                "date": commit["date"],
                "manifest": storable,
                "psf": psf_value if psf_value else psf(storable),
            }
    if prompt:
        new_state["last_notified_key"] = key
        new_state["last_notified_at"] = moment
    if write and not write_state(new_state):
        return None
    return update_block(form, commit, status, available, prompt,
                        None if verdict is None else verdict["detail"])


def diagnose(*, now: float | None = None) -> dict[str, Any]:
    """独立诊断：只读，不写状态、不改变提示闸门；含逐类路径预览（每类≤10）。"""
    moment = _now() if now is None else now
    repo = repo_slug()
    state = read_state()
    profile = detect_profile(repo=repo)
    form = profile["form"]
    out: dict[str, Any] = {
        "state_path": str(STATE_FILE),
        "lock_path": str(LOCK_FILE),
        "repo": repo,
        "enabled": enabled(),
        "form": form,
        "form_reason": profile["reason"],
        "form_evidence": profile["evidence"],
        "state": state,
        "due": check_due(moment, state),
        "writable": state_writable(),
    }
    if not out["enabled"]:
        return out
    if form == FORM_FOREIGN:
        return out

    hint: dict[str, Any] = {}
    commit = fetch_commit(repo, hint=hint)
    if commit is None:
        if hint.get("retry_at"):
            out["retry_at_hint"] = hint["retry_at"]
        return out
    out["remote"] = dict(commit)
    out["local_sha"] = local_commit_id(form)

    verdict: dict[str, Any] | None = None
    if form == FORM_BARE:
        snapshot = _snapshot_for(state, commit["sha"])
        if snapshot is not None:
            manifest = snapshot["manifest"]
            verdict = compare_snapshot(local_manifest(list(manifest)), manifest)
        else:
            tree = fetch_tree(repo, commit["sha"], hint=hint)
            if tree is None:
                if hint.get("retry_at"):
                    out["retry_at_hint"] = hint["retry_at"]
                return out
            out["remote"].update({k: tree[k] for k in ("psf", "paths")})
            out["remote"]["excluded"] = tree.get("excluded", 0)
            previous = state.get("snapshot")
            previous_manifest = previous.get("manifest") if isinstance(previous, dict) else None
            verdict = classify(tree["manifest"],
                               local_manifest(list(tree["manifest"]), expect=tree["manifest"]),
                               previous_manifest)
        status = "no_evidence" if verdict is None else verdict["status"]
    elif form == FORM_GIT:
        dirty = git_dirty_paths()
        local_id = out["local_sha"]
        if local_id is None or dirty is None:
            status = "no_evidence"
        elif local_id == commit["sha"]:
            status = "drift" if dirty else "synced"
        else:
            status = "conflict" if dirty else "behind"
        if dirty:
            out["dirty_paths"] = dirty[:10]
            verdict = {"status": status,
                       "detail": {"modified": dirty, "missing": [],
                                  "stale": [], "extra": []}}
    else:
        local_id = out["local_sha"]
        status = "no_evidence" if local_id is None else (
            "synced" if local_id == commit["sha"] else "behind")
    available = status not in ("synced", "no_evidence")
    key = f"{commit['sha']}:{status}"
    prompt = available and prompt_allowed(moment, state, key)
    out.update(
        available=available,
        status=status,
        prompt=prompt,
        block=update_block(form, commit, status, available, prompt,
                           None if verdict is None else verdict["detail"]),
    )
    if verdict is not None:
        detail = verdict["detail"]
        out["diff"] = {
            kind: {"total": len(paths), "paths": sorted(paths)[:10]}
            for kind, paths in detail.items()
        }
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(diagnose(), ensure_ascii=True, indent=1))
        return 0
    except Exception as exc:  # 诊断入口自身也不抛异常
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
