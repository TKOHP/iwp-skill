"""按安装形态执行 iwp 技能更新（维护面能力）。

用法（技能目录内）：
    python scripts/skill_update.py --preview        # 只探测并输出计划（默认）
    python scripts/skill_update.py --apply          # 执行更新（有覆盖风险时拒绝）
    python scripts/skill_update.py --apply --force  # 用户已确认覆盖风险后执行

三条路径（形态由 scripts/update_check.py 探测，不由文档或调用方指定）：
    git    git pull --ff-only
    npx    备份保活文件 -> npx skills update -> 回填 -> 哈希校验
    bare   按远端 sha 下载定版 zip -> 回滚副本 -> 原子覆盖 -> 删除过期未改文件

IWP_UPDATE_CHECK 是更新能力总开关：置 0/false/no/off 时检测与执行一起关闭，
本执行器（含 --preview/--apply）直接返回退出码 6，不探测、不发任何请求。

退出码：0 成功 / 1 失败 / 2 已是最新 / 3 需先确认 / 4 探测失败 / 5 拒绝执行
        / 6 已被配置禁用（IWP_UPDATE_CHECK=0）

方案：§3.4–§3.7（技能更新三形态探测与统一更新，见 docs 存档）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import skill_config  # noqa: E402

try:  # 作为包导入（测试）与直接运行两种方式都要工作
    from scripts import update_check as uc
except ImportError:  # pragma: no cover - 直接运行 scripts/skill_update.py
    import update_check as uc  # type: ignore[no-redef]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UPTODATE = 2
EXIT_CONFIRM = 3
EXIT_PROBE = 4
EXIT_REFUSED = 5
EXIT_DISABLED = 6  # IWP_UPDATE_CHECK=0：总开关关闭，未做任何探测

# === 执行参数 ===
GIT_PULL_TIMEOUT_S = 180.0
NPX_UPDATE_CMD = "npx -y skills@latest update iwp -g -y"
NPX_UPDATE_TIMEOUT_S = 300.0
ZIP_URL_FMT = "https://codeload.github.com/{repo}/zip/{sha}"
ZIP_DOWNLOAD_TIMEOUT_S = 120.0
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_ZIP_FILES = 5000
_OUTPUT_CAP = 4000


def skill_dir() -> Path:
    return _ROOT


def _run(args: list[str], cwd: Path, timeout: float) -> dict[str, Any]:
    """执行外部命令，返回 {rc, out, err}；不可用/超时 rc=-1。"""
    try:
        proc = subprocess.run(
            args, cwd=str(cwd), capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"rc": -1, "out": "", "err": f"{type(exc).__name__}: {exc}"}
    return {
        "rc": proc.returncode,
        "out": proc.stdout.decode("utf-8", "replace")[:_OUTPUT_CAP],
        "err": proc.stderr.decode("utf-8", "replace")[:_OUTPUT_CAP],
    }


def _run_shell(command: str, cwd: Path, timeout: float) -> dict[str, Any]:
    """执行固定命令串（不接受外部拼接值），shell 解析保证 Windows 找到 npx.cmd。"""
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), shell=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"rc": -1, "out": "", "err": f"{type(exc).__name__}: {exc}"}
    return {
        "rc": proc.returncode,
        "out": proc.stdout.decode("utf-8", "replace")[:_OUTPUT_CAP],
        "err": proc.stderr.decode("utf-8", "replace")[:_OUTPUT_CAP],
    }


def _is_link(path: Path) -> bool:
    """符号链接或 Windows 重解析点（junction）：覆盖式更新会写穿到源仓库。"""
    try:
        if path.is_symlink():
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_path(d: Path) -> Path:
    return d / ".update_state.json"


def _previous_manifest(d: Path) -> dict[str, str] | None:
    """上次一致时的发布集清单（过期文件删除规则的唯一依据）。"""
    state = uc.read_state(_state_path(d))
    snap = state.get("snapshot")
    if not isinstance(snap, dict):
        return None
    manifest = snap.get("manifest")
    if not isinstance(manifest, dict) or not manifest:
        return None
    return {k: v for k, v in manifest.items()
            if isinstance(k, str) and uc.valid_sha(v)}

# === 计划（探测 + 判定，无副作用）===

def make_plan(d: Path | None = None, repo: str | None = None) -> dict[str, Any]:
    base = skill_dir() if d is None else d
    target_repo = uc.repo_slug() if repo is None else repo
    profile = uc.detect_profile(base, repo=target_repo)
    form = profile["form"]
    plan: dict[str, Any] = {
        "skill_dir": str(base),
        "repo": target_repo,
        "form": form,
        "form_reason": profile["reason"],
        "form_evidence": profile["evidence"],
        "status": "unknown",
        "available": False,
        "local": None,
        "remote": None,
        "update_action": None,
        "needs_confirm": False,
        "notes": [],
        "_manifest": None,
        "_previous": None,
    }
    if form == uc.FORM_FOREIGN:
        plan["error"] = "foreign_repo"
        plan["notes"].append(
            "技能目录是 git 仓库但 origin 与目标仓库不一致（分叉或开发副本），不自动更新"
        )
        return plan

    keepalive = uc.keepalive_names(base / ".gitignore")
    if keepalive is None:
        plan["error"] = "keepalive_unavailable"
        plan["notes"].append(".gitignore 不可读，无法确定保活文件清单，拒绝覆盖式更新")
        return plan
    plan["keepalive"] = keepalive

    hint: dict[str, Any] = {}
    commit = uc.fetch_commit(target_repo, hint=hint)
    if commit is None:
        plan["error"] = "probe_failed"
        if hint.get("retry_at"):
            plan["retry_at_hint"] = hint["retry_at"]
        return plan
    plan["remote"] = dict(commit)

    if form == uc.FORM_BARE:
        tree = uc.fetch_tree(target_repo, commit["sha"], hint=hint)
        if tree is None:
            plan["error"] = "probe_failed"
            if hint.get("retry_at"):
                plan["retry_at_hint"] = hint["retry_at"]
            return plan
        plan["remote"].update({"psf": tree["psf"], "paths": tree["paths"]})
        previous = _previous_manifest(base)
        verdict = uc.classify(
            tree["manifest"],
            uc.local_manifest(list(tree["manifest"]), skill_dir=base,
                              expect=tree["manifest"]),
            previous,
        )
        plan["_manifest"] = tree["manifest"]
        plan["_previous"] = previous
        status = "no_evidence" if verdict is None else verdict["status"]
        plan["detail"] = None if verdict is None else {
            kind: len(paths) for kind, paths in verdict["detail"].items()
        }
        plan["update_action"] = "zip_overlay"
    elif form == uc.FORM_GIT:
        local = uc.local_commit_id(form, skill_dir=base)
        plan["local"] = {"id": local}
        dirty = uc.git_dirty_paths(base)
        if local is None or dirty is None:
            status = "no_evidence"
        elif local == commit["sha"]:
            status = "drift" if dirty else "synced"
        else:
            status = "conflict" if dirty else "behind"
        if dirty:
            plan["detail"] = {"modified": len(dirty), "missing": 0, "stale": 0, "extra": 0}
            plan["dirty_paths"] = dirty[:10]
        plan["update_action"] = "git_pull"
    else:  # npx
        local = uc.local_commit_id(form, skill_dir=base)
        plan["local"] = {"id": local}
        status = "no_evidence" if local is None else (
            "synced" if local == commit["sha"] else "behind")
        plan["update_action"] = "npx_update"
        plan["notes"].append(
            "npx 形态只能比对版本标识，无法判定本地文件是否被改动；"
            "更新会整体替换技能目录（保活文件会被备份回填）"
        )

    plan["status"] = status
    plan["available"] = status not in ("synced", "no_evidence")
    plan["needs_confirm"] = (
        status in ("drift", "conflict", "unknown")
        or form == uc.FORM_NPX
    )
    return plan


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith("_")}

# === 后置校验（方案 §3.4 / R6）===

def _content_fallback(d: Path, repo: str, sha: str) -> dict[str, Any] | None:
    """身份标识没跟上时的辅助证据：本地 SKILL.md 的 blob 是否等于远端那一项。"""
    tree = uc.fetch_tree(repo, sha)
    if tree is None:
        return None
    wanted = tree["manifest"].get("SKILL.md")
    if wanted is None:
        return None
    local = uc.local_manifest(["SKILL.md"], skill_dir=d)
    if local is None or local.get("SKILL.md") != wanted:
        return None
    return {"evidence": "skill_md_blob", "expected": wanted, "actual": local.get("SKILL.md")}


def post_check(form: str, d: Path, repo: str, sha: str,
               manifest: dict[str, str] | None = None) -> dict[str, Any]:
    """更新后独立核验：本目录是否已等于远端最新（不使用更新命令退出码）。"""
    if form == uc.FORM_GIT:
        rc, out = uc._run_git(["rev-parse", "HEAD"], cwd=d)
        actual = out if rc == 0 else None
        if actual == sha:
            return {"ok": True, "check": "git_head", "expected": sha, "actual": actual}
        fallback = _content_fallback(d, repo, sha)
        if fallback is not None:
            return {"ok": True, "check": "git_head", "expected": sha, "actual": actual,
                    "fallback": fallback}
        return {"ok": False, "check": "git_head", "expected": sha, "actual": actual}

    if form == uc.FORM_NPX:
        entry = uc._lock_entry()
        actual = entry.get("skillFolderHash") if entry else None
        if isinstance(actual, str) and actual.lower() == sha:
            return {"ok": True, "check": "lock_hash", "expected": sha, "actual": actual}
        fallback = _content_fallback(d, repo, sha)
        if fallback is not None:
            return {"ok": True, "check": "lock_hash", "expected": sha, "actual": actual,
                    "fallback": fallback}
        return {"ok": False, "check": "lock_hash", "expected": sha, "actual": actual}

    if manifest is None:
        return {"ok": False, "check": "release_set", "error": "manifest_missing"}
    local = uc.local_manifest(list(manifest), skill_dir=d, expect=manifest)
    if local is None:
        return {"ok": False, "check": "release_set", "error": "unreadable"}
    mismatched = sorted(p for p, want in manifest.items() if local.get(p) != want)
    extras = sorted(p for p in local if p not in manifest)
    return {
        "ok": not mismatched,
        "check": "release_set",
        "expected": sha,
        "paths": len(manifest),
        "mismatched": mismatched[:10],
        "mismatched_total": len(mismatched),
        "extras": extras[:10],
        "extras_total": len(extras),
    }

# === 三条更新路径 ===

def apply_git(d: Path, repo: str, sha: str) -> dict[str, Any]:
    result = _run(["git", "pull", "--ff-only"], d, GIT_PULL_TIMEOUT_S)
    steps = [{"step": "git_pull", "ok": result["rc"] == 0,
              "output": result["out"] or result["err"]}]
    if result["rc"] != 0:
        return {"ok": False, "steps": steps, "error": "git_pull_failed"}
    return {"ok": True, "steps": steps, "post_check": post_check(uc.FORM_GIT, d, repo, sha)}


def apply_npx(d: Path, repo: str, sha: str) -> dict[str, Any]:
    names = uc.keepalive_names(d / ".gitignore")
    if names is None:
        return {"ok": False, "steps": [], "error": "keepalive_unavailable"}
    present = [name for name in names if (d / name).is_file()]
    backup_dir = Path(tempfile.mkdtemp(prefix="iwp-skill-backup-"))
    backed: list[str] = []
    try:
        for name in present:
            try:
                shutil.copy2(d / name, backup_dir / name)
            except OSError as exc:
                return {"ok": False, "steps": [
                    {"step": "backup", "ok": False, "error": f"{name}: {exc}"}],
                    "error": "backup_failed", "backup_dir": str(backup_dir)}
            backed.append(name)
        steps: list[dict[str, Any]] = [
            {"step": "backup", "ok": True, "files": backed, "dir": str(backup_dir)}]

        update = _run_shell(NPX_UPDATE_CMD, d, NPX_UPDATE_TIMEOUT_S)
        steps.append({"step": "npx_update", "ok": update["rc"] == 0,
                      "output": update["out"] or update["err"]})

        restored: list[str] = []
        mismatched: list[str] = []
        for name in backed:
            try:
                shutil.copy2(backup_dir / name, d / name)
            except OSError:
                mismatched.append(name)
                continue
            restored.append(name)
            if _sha256(backup_dir / name) != _sha256(d / name):
                mismatched.append(name)
        steps.append({"step": "restore", "ok": not mismatched,
                      "files": restored, "mismatched": mismatched})
        if mismatched:
            return {"ok": False, "steps": steps, "error": "restore_mismatch",
                    "backup_dir": str(backup_dir)}
        shutil.rmtree(backup_dir, ignore_errors=True)
        steps.append({"step": "cleanup_backup", "ok": True})
        check = post_check(uc.FORM_NPX, d, repo, sha)
        return {"ok": check["ok"], "steps": steps, "post_check": check}
    except OSError as exc:
        # 备份目录保留给人工核验：它含凭据副本，不静默丢弃
        return {"ok": False, "steps": [], "error": f"backup_failed: {exc}",
                "backup_dir": str(backup_dir)}


def _download(url: str, dest: Path) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "iwp-skill-update/3"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=ZIP_DOWNLOAD_TIMEOUT_S) as resp:
            with dest.open("wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ZIP_BYTES:
                        return {"ok": False, "error": "zip_too_large"}
                    fh.write(chunk)
    except Exception as exc:  # noqa: BLE001 - 下载失败一律转为结构化错误
        return {"ok": False, "error": f"download_failed: {type(exc).__name__}: {exc}"}
    return {"ok": True, "bytes": total}


def _safe_extract(archive: Path, dest: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(archive) as zf:
            count = 0
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
                    return {"ok": False, "error": "unsafe_zip_entry"}
                count += 1
                if count > MAX_ZIP_FILES:
                    return {"ok": False, "error": "zip_too_many_files"}
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, 65536)
    except (zipfile.BadZipFile, OSError) as exc:
        return {"ok": False, "error": f"unzip_failed: {exc}"}
    return {"ok": True, "files": count}


def _zip_root(extracted: Path) -> Path | None:
    """codeload 压缩包只有一个顶层目录 `<repo>-<sha>/`，剥掉它。"""
    entries = [p for p in extracted.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return None


def apply_bare(d: Path, repo: str, sha: str,
               manifest: dict[str, str],
               previous: dict[str, str] | None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    work = Path(tempfile.mkdtemp(prefix="iwp-skill-rollback-"))
    tmp_root = Path(tempfile.mkdtemp(prefix="iwp-skill-download-"))
    archive = tmp_root / "release.zip"
    try:
        url = ZIP_URL_FMT.format(repo=repo, sha=sha)
        got = _download(url, archive)
        if not got["ok"]:
            return {"ok": False, "steps": steps, "error": got["error"], "url": url}
        steps.append({"step": "download", "ok": True, "bytes": got["bytes"], "url": url})

        extracted = tmp_root / "extract"
        extracted.mkdir(parents=True, exist_ok=True)
        unzip = _safe_extract(archive, extracted)
        if not unzip["ok"]:
            return {"ok": False, "steps": steps, "error": unzip["error"]}
        root = _zip_root(extracted)
        if root is None or not (root / "SKILL.md").is_file():
            return {"ok": False, "steps": steps, "error": "invalid_release_archive"}
        steps.append({"step": "extract", "ok": True, "files": unzip["files"]})

        names = uc.keepalive_names(d / ".gitignore") or []
        keep = set(names)
        replaced: list[str] = []
        created: list[str] = []
        try:
            for src in root.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(root).as_posix()
                if rel in keep:
                    continue
                dst = d / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_file():
                    backup = work / "replaced" / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, backup)
                else:
                    created.append(rel)
                staged = dst.with_name(dst.name + ".iwp-new")
                shutil.copyfile(src, staged)
                os.replace(staged, dst)
                replaced.append(rel)
            steps.append({"step": "overlay", "ok": True, "files": len(replaced)})

            deleted, kept_modified = _prune_stale(d, previous, manifest, keep, work)
            steps.append({"step": "prune_stale", "ok": True,
                          "deleted": deleted[:20], "deleted_total": len(deleted),
                          "kept_modified": kept_modified[:20],
                          "kept_modified_total": len(kept_modified)})
        except OSError as exc:
            restored = _rollback(d, work, replaced, created)
            return {"ok": False, "steps": steps, "error": f"overlay_failed: {exc}",
                    "rollback": {"restored": len(restored), "dir": str(work)}}

        check = post_check(uc.FORM_BARE, d, repo, sha, manifest)
        if check["ok"]:
            shutil.rmtree(work, ignore_errors=True)
            return {"ok": True, "steps": steps, "post_check": check}
        return {"ok": False, "steps": steps, "post_check": check,
                "rollback": {"available": True, "dir": str(work)}}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _prune_stale(d: Path, previous: dict[str, str] | None,
                 manifest: dict[str, str], keep: set[str],
                 work: Path) -> tuple[list[str], list[str]]:
    """删除「上次快照里有、新发布集没有、且本地未改」的文件（方案 R11）。

    返回 (已删除, 因本地已改而保留)。删除前先复制到回滚目录。
    """
    deleted: list[str] = []
    kept_modified: list[str] = []
    if not previous:
        return deleted, kept_modified
    for rel, want in previous.items():
        if rel in manifest or rel in keep:
            continue
        target = d / rel
        if not target.is_file():
            continue
        try:
            digest = uc.blob_sha(target.read_bytes())
        except OSError:
            continue
        if digest != want:
            kept_modified.append(rel)
            continue
        backup = work / "deleted" / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(target, backup)
            target.unlink()
        except OSError:
            continue
        deleted.append(rel)
    return deleted, kept_modified


def _rollback(d: Path, work: Path, replaced: list[str], created: list[str]) -> list[str]:
    """用回滚副本恢复已替换文件，删除本次新建文件。返回已恢复的路径。"""
    restored: list[str] = []
    for rel in replaced:
        backup = work / "replaced" / rel
        target = d / rel
        try:
            if backup.is_file():
                shutil.copy2(backup, target)
                restored.append(rel)
        except OSError:
            continue
    for rel in created:
        target = d / rel
        try:
            if target.is_file():
                target.unlink()
        except OSError:
            continue
    deleted_backup = work / "deleted"
    if deleted_backup.is_dir():
        for src in deleted_backup.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(deleted_backup)
            target = d / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                restored.append(rel.as_posix())
            except OSError:
                continue
    return restored

# === 编排 ===

def run(d: Path | None = None, repo: str | None = None, *, apply: bool = False,
        force: bool = False) -> dict[str, Any]:
    base = skill_dir() if d is None else d
    # 总开关：关闭时连 --preview 也不联网、不判形态，直接拒绝（方案 §3.9）
    if not uc.enabled():
        return {
            "ok": False,
            "exit": EXIT_DISABLED,
            "error": "update_disabled",
            "message": "IWP_UPDATE_CHECK=0：更新检测与更新执行均已关闭，"
                       "本次未发起任何请求；如需更新，先把 .env 中该开关置 1",
        }
    plan = make_plan(base, repo)
    out: dict[str, Any] = {"plan": public_plan(plan)}
    if plan.get("error"):
        out.update(ok=False, exit=EXIT_PROBE if plan["error"] == "probe_failed"
                   else EXIT_REFUSED, error=plan["error"])
        return out
    if _is_link(base) and plan["form"] != uc.FORM_GIT:
        out.update(ok=False, exit=EXIT_REFUSED, error="skill_dir_is_link")
        out["plan"]["notes"].append(
            "技能目录是链接/重解析点，覆盖式更新会写穿到源目录，已拒绝")
        return out
    if not plan["available"]:
        out.update(ok=True, exit=EXIT_UPTODATE, status=plan["status"],
                   message="already up to date" if plan["status"] == "synced"
                   else "no evidence to compare")
        return out
    if not apply:
        out.update(ok=True, exit=EXIT_CONFIRM if plan["needs_confirm"] else EXIT_OK,
                   needs_confirm=plan["needs_confirm"])
        return out
    if plan["needs_confirm"] and not force:
        out.update(ok=False, exit=EXIT_CONFIRM, error="confirm_required",
                   message="更新存在覆盖风险，先向用户展示 preview 并取得确认后加 --force")
        return out

    form = plan["form"]
    sha = plan["remote"]["sha"]
    if form == uc.FORM_GIT:
        result = apply_git(base, plan["repo"], sha)
    elif form == uc.FORM_NPX:
        result = apply_npx(base, plan["repo"], sha)
    else:
        result = apply_bare(base, plan["repo"], sha, plan["_manifest"], plan["_previous"])

    ok = bool(result.get("ok"))
    out.update(ok=ok, exit=EXIT_OK if ok else EXIT_FAIL,
               action=plan["update_action"],
               status_before=plan["status"],
               steps=result.get("steps"),
               post_check=result.get("post_check"),
               notes=plan.get("notes", []))
    for key in ("error", "backup_dir", "rollback", "url"):
        if result.get(key) is not None:
            out[key] = result[key]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按安装形态更新 iwp 技能")
    parser.add_argument("--preview", action="store_true",
                        help="只探测并输出计划（默认行为）")
    parser.add_argument("--apply", action="store_true", help="执行更新")
    parser.add_argument("--force", action="store_true",
                        help="用户已确认覆盖风险（drift/conflict/npx 形态需要）")
    parser.add_argument("--dir", default=None, help="技能目录（默认本脚本所在技能）")
    parser.add_argument("--repo", default=None, help="owner/repo（默认 IWP_UPDATE_REPO）")
    args = parser.parse_args(argv)

    target = Path(args.dir).resolve() if args.dir else skill_dir()
    try:
        payload = run(target, args.repo, apply=args.apply, force=args.force)
    except Exception as exc:  # 执行入口自身也不抛异常
        payload = {"ok": False, "exit": EXIT_FAIL,
                   "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(payload, ensure_ascii=True, indent=1))
    return int(payload.get("exit", EXIT_FAIL))


if __name__ == "__main__":
    sys.exit(main())
