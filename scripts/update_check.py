"""检测 iwp 技能上游是否有更新（维护面能力，见 SKILL.md 第 0 步）。

定位：维护面能力，不参与业务链路。纯标准库、尽力而为——任何异常都不向
调用方抛出，不改变 preflight 的 ok 判定与退出码；每天最多发起一次匿名
GitHub 请求，失败后 24 小时内不重试。

判定基准：发布集指纹 PSF（Publish-Set Fingerprint）
    远端：Tree API 列出技能目录下所有被 git 跟踪的 blob（路径 -> blob sha）
    本地：git 形态用 `git hash-object`（应用 clean filter，消除行尾差异）；
          普通目录形态按 `sha1("blob <字节数>\\0" + 内容)` 复算
    逐路径比对得出 synced / behind / drift / conflict / unknown；
    PSF 只作为状态指纹记录，不参与跨实现比较。
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
DEFAULT_REPO = "TKOHP/iwp-skill"

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
STATE_SCHEMA = 2
STATE_KEYS = (
    "schema", "repo", "last_check_at", "last_result", "last_error",
    "remote_sha", "remote_date", "psf", "manifest",
    "last_notified_key", "last_notified_at", "retry_at",
)

# === 发布集上限（方案 §4.7 R12）===
MAX_MANIFEST_PATHS = 2000        # 超过只记 psf，不再记逐路径清单
MAX_MANIFEST_BYTES = 512 * 1024  # 清单 JSON 体积上限
MAX_FILE_BYTES = 4 * 1024 * 1024  # 单文件超过即视为运行产物，不进发布集

_OFF_VALUES = {"", "0", "false", "no", "off"}
_STATES = ("synced", "behind", "drift", "conflict", "unknown")


def enabled() -> bool:
    """IWP_UPDATE_CHECK：默认开；0/false/no/off 关闭整项能力。"""
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
    """给 agent 的动作标签：git 形态走 git pull，其余走 update 路由。"""
    return "git_pull" if form == "git" else "update_route"


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

# === 远端探测（匿名、两次请求、硬预算）===
API_COMMIT_FMT = "https://api.github.com/repos/{repo}/commits/main"
API_TREE_FMT = "https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
HTTP_TIMEOUT_S = 15.0       # urllib 单次请求超时（实测 1.9–9.7s，跨境链路波动极大）
PROBE_BUDGET_S = 30.0       # 整个探测（commit + tree）的硬截止，超出即静默放弃
MAX_RESPONSE_BYTES = 256 * 1024
TREE_MAX_BYTES = 1024 * 1024
_USER_AGENT = "iwp-skill-update-check/2"


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


def fetch_remote_manifest(repo: str, *, budget_s: float = PROBE_BUDGET_S,
                          fetch: Any = None,
                          hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """取远端发布集；任何失败/超预算返回 None（静默）。

    hint 若传入，配额耗尽时会写入 {"retry_at": <epoch>}，调用方据此退避。
    """
    import threading

    if not valid_repo(repo):
        return None
    fetch = fetch or _http_get_json
    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            commit = parse_remote(
                _fetch_with_retry(fetch, API_COMMIT_FMT.format(repo=repo))
            )
            if commit is None:
                box["failed"] = True
                return
            tree = parse_tree(_fetch_with_retry(
                fetch, API_TREE_FMT.format(repo=repo, sha=commit["sha"]),
                TREE_MAX_BYTES,
            ))
            if tree is None:
                box["failed"] = True
                return
            box["ok"] = {**commit, **tree}
        except RateLimited as exc:
            box["failed"] = True
            box["retry_at"] = exc.retry_at
        except Exception:
            box["failed"] = True

    worker = threading.Thread(target=_work, name="iwp-update-probe", daemon=True)
    worker.start()
    worker.join(budget_s)
    if hint is not None and isinstance(box.get("retry_at"), (int, float)):
        hint["retry_at"] = float(box["retry_at"])
    if worker.is_alive() or box.get("failed"):
        return None
    return box.get("ok")

# === 本地发布集（git hash-object / 原始字节复算）===
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
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def _git_manifest(paths: list[str], skill_dir: Path, run: Any) -> dict[str, str] | None:
    """`git hash-object --stdin-paths` 批量取 blob sha（自动应用 clean filter）。"""
    if any("\n" in p for p in paths):
        return None
    present = [p for p in paths if (skill_dir / p).is_file()]
    out: dict[str, str] = {}
    for start in range(0, len(present), 200):
        part = present[start:start + 200]
        rc, text = run(["hash-object", "--stdin-paths"], cwd=skill_dir,
                       stdin_data="\n".join(part) + "\n")
        if rc != 0:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) != len(part):
            return None
        for path, sha in zip(part, lines):
            if not valid_sha(sha):
                return None
            out[path] = sha
    return out


def _raw_manifest(paths: list[str], skill_dir: Path,
                 expect: dict[str, str] | None = None) -> dict[str, str] | None:
    """普通目录：直接读字节复算 blob sha；任一文件不可读则整体无证据。

    Windows 检出的副本行尾为 CRLF，而仓库里存的是 LF；原始字节不等且
    期望值存在时，再按 CRLF->LF 归一复算一次（只影响文本，二进制无 CRLF
    不受影响），避免整包假差异。
    """
    out: dict[str, str] = {}
    for path in paths:
        target = skill_dir / path
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


def detect_form(skill_dir: Path | None = None) -> str:
    d = SKILL_DIR if skill_dir is None else skill_dir
    try:
        inside_git = (d / ".git").exists()
    except OSError:
        inside_git = False
    return "git" if inside_git else "cli"


def local_manifest(paths: list[str], *, skill_dir: Path | None = None,
                   form: str | None = None, run: Any = None,
                   expect: dict[str, str] | None = None) -> dict[str, str] | None:
    d = SKILL_DIR if skill_dir is None else skill_dir
    kind = detect_form(d) if form is None else form
    if kind == "git":
        return _git_manifest(paths, d, run or _run_git)
    return _raw_manifest(paths, d, expect)


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


def _git_identity(repo: str, skill_dir: Path, run: Any) -> bool | None:
    """origin 与目标仓库一致才算适用；不一致返回 False（静默且退避）。"""
    rc, origin = run(["remote", "get-url", "origin"], cwd=skill_dir)
    if rc != 0:
        return None
    return parse_github_repo(origin) == repo


def _lock_identity(repo: str, lock_path: Path) -> bool | None:
    """lock 记录 GitHub 来源且仓库一致才算适用；无 lock 或非 GitHub 返回 None。"""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    skills = data.get("skills")
    entry = skills.get("iwp") if isinstance(skills, dict) else None
    if not isinstance(entry, dict) or entry.get("sourceType") != "github":
        return None
    locked = parse_github_repo(entry.get("sourceUrl")) or entry.get("source")
    if locked is None:
        return None
    return locked == repo


def identity(form: str, repo: str, *, skill_dir: Path | None = None,
             lock_path: Path | None = None, run: Any = None) -> bool | None:
    d = SKILL_DIR if skill_dir is None else skill_dir
    if form == "git":
        return _git_identity(repo, d, run or _run_git)
    return _lock_identity(repo, lock_path or LOCK_FILE)

# === 编排（唯一对外入口；永不抛异常）===

def maybe_check(*, now: float | None = None,
                write: bool = True) -> dict[str, Any] | None:
    """会话首次调用前运行：需要时返回 update 块，否则返回 None。"""
    try:
        return _maybe_check(now=now, write=write)
    except Exception:
        return None


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
    form = detect_form()
    usable = identity(form, repo)
    if usable is None:
        if write:
            write_state({**state, "last_check_at": moment,
                         "last_result": "no_evidence", "last_error": "no_identity",
                         "retry_at": moment + FAIL_RETRY_S})
        return None
    if usable is False:
        if write:
            write_state({**state, "last_check_at": moment,
                         "last_result": "error", "last_error": "repo_mismatch",
                         "retry_at": moment + FAIL_RETRY_S})
        return None
    hint: dict[str, Any] = {}
    remote = fetch_remote_manifest(repo, hint=hint)
    if remote is None:
        retry_at = hint.get("retry_at") or moment + FAIL_RETRY_S
        if write:
            write_state({**state, "last_check_at": moment,
                         "last_result": "error", "last_error": "probe_failed",
                         "retry_at": retry_at})
        return None
    local = local_manifest(list(remote["manifest"]), form=form,
                           expect=remote["manifest"])
    verdict = classify(remote["manifest"], local, state.get("manifest"))
    status = "no_evidence" if verdict is None else verdict["status"]
    available = status not in ("synced", "no_evidence")
    key = f"{remote['sha']}:{status}"
    prompt = available and prompt_allowed(moment, state, key)
    # manifest/psf 只记录「上次一致时的快照」（方案 §4.6）：未同步时保留旧值，
    # 否则下一轮会把「本地仍是旧版」误判成 drift。
    synced = status == "synced"
    new_state = {
        **state,
        "last_check_at": moment,
        "last_result": status,
        "last_error": None,
        "remote_sha": remote["sha"],
        "remote_date": remote["date"],
        "retry_at": None,
        "psf": remote["psf"] if synced else state.get("psf"),
        "manifest": (storable_manifest(remote["manifest"]) if synced
                     else state.get("manifest")),
    }
    if prompt:
        new_state["last_notified_key"] = key
        new_state["last_notified_at"] = moment
    if write and not write_state(new_state):
        return None
    return update_block(form, remote, status, available, prompt,
                        None if verdict is None else verdict["detail"])


def diagnose(*, now: float | None = None) -> dict[str, Any]:
    """独立诊断：只读，不写状态、不改变提示闸门；含逐类路径预览（每类≤10）。"""
    moment = _now() if now is None else now
    repo = repo_slug()
    state = read_state()
    form = detect_form()
    out: dict[str, Any] = {
        "state_path": str(STATE_FILE),
        "lock_path": str(LOCK_FILE),
        "repo": repo,
        "enabled": enabled(),
        "form": form,
        "state": state,
        "due": check_due(moment, state),
        "writable": state_writable(),
    }
    if not out["enabled"]:
        return out
    usable = identity(form, repo)
    out["identity"] = usable
    if usable is not True:
        return out
    hint: dict[str, Any] = {}
    remote = fetch_remote_manifest(repo, hint=hint)
    if remote is None and hint.get("retry_at"):
        out["retry_at_hint"] = hint["retry_at"]
    out["remote"] = None if remote is None else {
        "sha": remote["sha"], "date": remote["date"],
        "psf": remote["psf"], "paths": remote["paths"],
        "excluded": remote.get("excluded", 0),
    }
    if remote is None:
        return out
    local = local_manifest(list(remote["manifest"]), form=form,
                           expect=remote["manifest"])
    verdict = classify(remote["manifest"], local, state.get("manifest"))
    status = "no_evidence" if verdict is None else verdict["status"]
    available = status not in ("synced", "no_evidence")
    key = f"{remote['sha']}:{status}"
    out.update(
        available=available,
        status=status,
        prompt=available and prompt_allowed(moment, state, key),
        block=update_block(form, remote, status, available,
                           available and prompt_allowed(moment, state, key),
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
