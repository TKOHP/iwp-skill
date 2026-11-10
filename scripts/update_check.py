"""检测 iwp 技能上游是否有更新（维护面能力，见 SKILL.md 第 0 步）。

定位：维护面能力，不参与业务链路。纯标准库、尽力而为——
任何异常都不向调用方抛出，不改变 preflight 的 ok 判定与退出码，
每天最多发起一次匿名 GitHub 请求，失败后 24 小时内不重试。

形态（由本地证据自动判定，不需要用户声明）：
    git  技能目录本身是 git 工作区（clone / symlink 指向 clone）；
         用 origin 仓库身份 + `git merge-base --is-ancestor` 判定
    cli  技能目录是 npx skills 安装的普通目录；
         用 %USERPROFILE%\\.agents\\.skill-lock.json 的 iwp 条目判定
"""
from __future__ import annotations

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

# === 时间预算（方案 §5.3 / §8）===
CHECK_INTERVAL_S = 24 * 3600    # 两次真实探测的最小间隔
GATE_INTERVAL_S = 24 * 3600     # 同一远端指纹的重复提示间隔
MAX_CLOCK_SKEW_S = 3600         # 状态时间戳超前该值视为时钟异常，重新探测

# === 状态文件契约 ===
STATE_SCHEMA = 1
STATE_KEYS = (
    "schema", "repo", "last_check_at", "last_result", "last_error",
    "known_remote_sha", "last_notified_sha", "last_notified_at",
)

_OFF_VALUES = {"", "0", "false", "no", "off"}


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
            json.dump(payload, fh, ensure_ascii=True, indent=1)
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


def check_due(now: float, state: dict[str, Any], interval: float = CHECK_INTERVAL_S) -> bool:
    """24h 门禁：上次尝试在窗内则跳过；时间戳缺失/超前则重新探测。"""
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


def action_for(form: str) -> str:
    """给 agent 的动作标签：git 形态走 git pull，其余走 update 路由。"""
    return "git_pull" if form == "git" else "update_route"


def prompt_allowed(now: float, state: dict[str, Any], remote_sha: str) -> bool:
    """提示闸门：同一远端指纹 24h 内不重复提示；指纹变化可立即再提示。"""
    if not valid_sha(remote_sha):
        return False
    if state.get("last_notified_sha") != remote_sha:
        return True
    at = state.get("last_notified_at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True
    if at > now + MAX_CLOCK_SKEW_S:
        return True
    return now - at >= GATE_INTERVAL_S


def decide(now: float, state: dict[str, Any], remote_sha: str,
           evidence: bool | None) -> tuple[bool, bool]:
    """证据三态 -> (available, prompt)。

    evidence: True=本地已包含远端提交；False=落后；None=无证据（不提示）。
    """
    available = evidence is False
    prompt = available and prompt_allowed(now, state, remote_sha)
    return available, prompt


def update_block(form: str, remote: dict[str, str], available: bool,
                 prompt: bool) -> dict[str, Any]:
    return {
        "checked": True,
        "available": available,
        "form": form,
        "remote_sha": remote["sha"],
        "remote_date": remote["date"],
        "action": action_for(form),
        "prompt": prompt,
    }

# === 远端探测（匿名、一次一个请求、硬预算）===
API_URL_FMT = "https://api.github.com/repos/{repo}/commits/main"
HTTP_TIMEOUT_S = 2.0        # urllib 单次请求超时
PROBE_BUDGET_S = 2.5        # 整个探测的硬截止（方案 §8 效率预算）
MAX_RESPONSE_BYTES = 256 * 1024
_USER_AGENT = "iwp-skill-update-check/1"


def _http_get_json(url: str, timeout: float = HTTP_TIMEOUT_S,
                   max_bytes: int = MAX_RESPONSE_BYTES) -> Any:
    """GET 并解析 JSON；异常类型不收敛（由调用方统一吞掉）。"""
    import urllib.request

    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"response too large: >{max_bytes} bytes")
    return json.loads(raw.decode("utf-8"))


def fetch_remote(repo: str, *, budget_s: float = PROBE_BUDGET_S,
                 fetch: Any = None) -> dict[str, str] | None:
    """取远端 main 指纹；任何失败/超预算返回 None（静默，24h 内不重试）。"""
    import threading

    if not valid_repo(repo):
        return None
    fetch = fetch or _http_get_json
    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            box["payload"] = fetch(API_URL_FMT.format(repo=repo))
        except Exception:
            box["failed"] = True

    worker = threading.Thread(target=_work, name="iwp-update-probe", daemon=True)
    worker.start()
    worker.join(budget_s)
    if worker.is_alive() or box.get("failed"):
        return None
    return parse_remote(box.get("payload"))

# === 本地证据链（git / lock）===
GIT_TIMEOUT_S = 2.0


def _run_git(args: list[str], *, cwd: Path,
             timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    """执行 git；不可用/超时统一返回 (-1, "")。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return proc.returncode, proc.stdout.strip()


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


def detect_form(skill_dir: Path | None = None) -> str:
    d = SKILL_DIR if skill_dir is None else skill_dir
    try:
        inside_git = (d / ".git").exists()
    except OSError:
        inside_git = False
    return "git" if inside_git else "cli"


def _git_evidence(remote_sha: str, skill_dir: Path, repo: str,
                  run: Any) -> bool | None:
    rc, origin = run(["remote", "get-url", "origin"], cwd=skill_dir)
    if rc != 0 or parse_github_repo(origin) != repo:
        return None
    rc, _ = run(["merge-base", "--is-ancestor", remote_sha, "HEAD"],
                cwd=skill_dir)
    if rc == 0:
        return True
    if rc < 0:
        return None
    # origin 已确认后，merge-base 非 0 一律视为「本地未包含」：含缺失对象(浅克隆)、
    # 历史分叉与远端 commit 非祖先(方案 §5.4 / R7)。
    return False


def _lock_evidence(remote_sha: str, lock_path: Path, repo: str) -> bool | None:
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
    if locked != repo:
        return None
    fingerprint = entry.get("skillFolderHash")
    if not valid_sha(fingerprint):
        return None
    return fingerprint == remote_sha


def local_evidence(remote_sha: str, *, skill_dir: Path | None = None,
                   lock_path: Path | None = None, repo: str | None = None,
                   run: Any = None) -> tuple[str, bool | None]:
    """返回 (形态, 证据)：True=本地已包含该提交；False=落后；None=无证据。"""
    d = SKILL_DIR if skill_dir is None else skill_dir
    slug = repo or repo_slug()
    form = detect_form(d)
    if form == "git":
        return form, _git_evidence(remote_sha, d, slug, run or _run_git)
    return form, _lock_evidence(remote_sha, lock_path or LOCK_FILE, slug)

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
    remote = fetch_remote(repo)
    if remote is None:
        if write:
            write_state({**state, "last_check_at": moment,
                         "last_result": "error", "last_error": "probe_failed"})
        return None
    sha = remote["sha"]
    form, evidence = local_evidence(sha, repo=repo)
    available, prompt = decide(moment, state, sha, evidence)
    if evidence is None:
        result = "no_evidence"
    elif available:
        result = "behind"
    else:
        result = "synced"
    new_state = {**state, "last_check_at": moment, "last_result": result,
                 "last_error": None, "known_remote_sha": sha}
    if prompt:
        new_state["last_notified_sha"] = sha
        new_state["last_notified_at"] = moment
    if write and not write_state(new_state):
        return None
    return update_block(form, remote, available, prompt)


def diagnose(*, now: float | None = None) -> dict[str, Any]:
    """独立诊断：只读，不写状态、不改变提示闸门。"""
    moment = _now() if now is None else now
    repo = repo_slug()
    state = read_state()
    out: dict[str, Any] = {
        "state_path": str(STATE_FILE),
        "lock_path": str(LOCK_FILE),
        "repo": repo,
        "enabled": enabled(),
        "state": state,
        "due": check_due(moment, state),
        "writable": state_writable(),
    }
    remote = fetch_remote(repo) if out["enabled"] else None
    out["remote"] = remote
    if remote:
        form, evidence = local_evidence(remote["sha"], repo=repo)
        available, prompt = decide(moment, state, remote["sha"], evidence)
        out.update(form=form, evidence=evidence, available=available,
                   prompt=prompt,
                   block=update_block(form, remote, available, prompt))
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
