#!/usr/bin/env python3
"""
记忆迁移导出脚本 v0.2 — Zero/Alpha 双实例通用
导出某实例的全部"该带走的记忆资产"到 bundle 目录（JSONL 语义中立, 不含 embedding/secrets）。

用法:
  python3 export_memory_bundle.py --app c2a5c8e8 [--out bundle_zero_v0.2]
  python3 export_memory_bundle.py --app 5052c33a --out bundle_alpha_v0.2

导出内容:
  cognitions.jsonl    认知相 chunks (phase='cognition', 全字段, embedding 置空)
  associations.jsonl  认知关联边
  memory_layers.jsonl 分层经历 (memory_layers.db 全表)
  open_todos.jsonl    global_todos 中未完结 todos + plans + notes + triggers (不含 sessions)
  memory_files/       rules/lessons/context 等记忆 md + entity_index.json + typical_cases.json
  persona/            LIFE_PERSONA.md
  config/             routines.yaml subscriptions.yaml app.yaml(脱敏)
  skills/  tools/     个人技能与工具源码 (排除 __pycache__)
  workspace/          工作空间 (排除 tmp/logs/cache/sessions_dumps)
  manifest.json       清单+sha256+行数+脱敏记录

明确不带: secrets.env / social.env / sessions_dumps/ / runtime_log.db / llm_payload_dumps /
          var/ run/ logs/ *.bak* *.legacy *.archive / data/*.db (memory_vectors/memory_layers 原库)
"""
import argparse, hashlib, json, re, shutil, sqlite3, sys, time
from pathlib import Path

SENSITIVE_NAME_RE = re.compile(
    r'^[A-Za-z0-9_]*(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|APIKEY)\s*[=:]',
    re.IGNORECASE)
# yaml/json 值形态的敏感词
SENSITIVE_VALUE_RE = re.compile(
    r'(api_key|apikey|token|secret|password|passwd|credential)\s*[:=]',
    re.IGNORECASE)

def find_project_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / 'data' / 'global_todos.db').exists() or (p / 'CLAUDE.md').exists():
            return p
    raise SystemExit('找不到项目根 (向上找 data/global_todos.db 失败)')

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()[:16]

# 右侧是"从环境/配置读"的代码形态 → 不含真值, 不打码 (避免误伤源码)
SAFE_VALUE_RE = re.compile(
    r'os\.environ|getenv|load_dotenv|config\[|cfg\.|\bget\(|Settings\(|Field\(|'
    r'\*\*|\$\{|\{\{|%s|%d|None|true|false|null')

def mask_line(line: str) -> str:
    m = re.match(r'^(\s*[\w.\-]+\s*[:=]\s*)(.+)$', line)
    if not m:
        return line
    value = m.group(2).strip()
    # 字面量才算泄漏: 引号字符串 / 裸十六进制或长随机串; 且不是安全代码形态
    is_literal = value.startswith(('"', "'", '`')) or re.fullmatch(
        r'[A-Za-z0-9_\-]{16,}', value) is not None
    if not is_literal or SAFE_VALUE_RE.search(value):
        return line
    nl = '\n' if line.endswith('\n') else ''
    return m.group(1) + '"***REDACTED***"' + nl

BUNDLE_OUT = None

def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except (ValueError, TypeError):
        return False

def copy_tree_filtered(src: Path, dst: Path, exclude_re: re.Pattern, manifest: dict, label: str,
                       sensitive_scan=False, exclude_under=None):
    if not src.exists():
        manifest['sections'][label] = {'status': 'missing'}
        return
    # 修⑧⑨⑫: exclude_under 统一为列表; 修⑫ 排除泛化——双布局(memory_migration + workspace/migration)×任意 bundle_* 前缀
    # (防两类嵌套: 本次输出目录自嵌套 + workspace 里躺着的旧版 bundle 被当普通内容打包)
    _ex = list(exclude_under) if isinstance(exclude_under, (list, tuple)) else ([exclude_under] if exclude_under else [])
    _mm_roots = [d for d in (src / 'memory_migration', src / 'workspace' / 'migration', src / 'workspace' / 'memory_migration') if d.is_dir()]
    _bundle_dirs = [b for r in _mm_roots for b in r.glob('bundle_*')]
    _ex_all = _ex + _bundle_dirs
    copied, redacted_files = 0, []
    for f in sorted(src.rglob('*')):
        rel = f.relative_to(src)
        if f.is_dir():
            continue
        s = str(rel)
        if exclude_re.search(s):
            continue
        if any((lambda eu: (f.resolve().relative_to(eu), False)[1] if False else _under(f.resolve(), eu))(eu) for eu in _ex_all if eu):
            continue
        target = dst / 'root_placeholder' if False else Path(str(dst).replace('root_placeholder','')) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 修①(Alpha复审): suffix 只取最后一段, .py.bak.0826 → '.0826' 整体逃过打码
        # 改为"文件名含点即文本候选"(多级后缀全命中), 排除二进制/媒体后缀
        _BIN = ('.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.gz', '.db', '.sqlite',
                '.wav', '.mp3', '.m4a', '.bin', '.pkl', '.npy', '.parquet',
                '.docx', '.xlsx', '.pptx', '.ico', '.svg', '.woff', '.ttf')
        if sensitive_scan and '.' in f.name and f.suffix not in _BIN:
            text = f.read_text(encoding='utf-8', errors='replace')
            lines = text.splitlines(keepends=True)
            out, hit = [], False
            for ln in lines:
                if SENSITIVE_NAME_RE.match(ln.strip()):
                    out.append(mask_line(ln)); hit = True
                elif SENSITIVE_VALUE_RE.search(ln):
                    out.append(mask_line(ln)); hit = True
                else:
                    out.append(ln)
            target.write_text(''.join(out), encoding='utf-8')
            if hit:
                redacted_files.append(s)
        else:
            shutil.copy2(f, target)
        copied += 1
    manifest['sections'][label] = {
        'status': 'ok', 'files': copied,
        'redacted': sorted(set(redacted_files)) if sensitive_scan else [],
    }

def export_db_jsonl(db_path: Path, sql: str, out_path: Path, manifest: dict, label: str,
                    drop_cols=('embedding',)):
    if not db_path.exists():
        manifest['sections'][label] = {'status': 'missing-db'}
        return
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql).fetchall()
    n = 0
    with out_path.open('w', encoding='utf-8') as f:
        for r in rows:
            d = dict(r)
            for c in drop_cols:
                d.pop(c, None)
            # bytes 兜底转 hex 字符串
            for k, v in d.items():
                if isinstance(v, bytes):
                    d[k] = v.hex()
            f.write(json.dumps(d, ensure_ascii=False) + '\n')
            n += 1
    con.close()
    manifest['sections'][label] = {'status': 'ok', 'rows': n,
                                   'sha256': sha256_file(out_path)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', required=True, help='实例短 id, 如 c2a5c8e8')
    ap.add_argument('--out', default=None, help='输出目录')
    ap.add_argument('--root', default=None, help='项目根 (默认自动探测)')
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    root = Path(args.root) if args.root else find_project_root(script_dir)
    apps = list((root / 'apps').glob(f'{args.app}*'))
    if not apps:
        raise SystemExit(f'apps/ 下找不到实例 {args.app}*')
    app_dir = apps[0]
    out = Path(args.out) if args.out else script_dir / f'bundle_{args.app}_v0.2'
    global BUNDLE_OUT
    BUNDLE_OUT = out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    manifest = {
        'schema_version': '0.2',
        'exported_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'instance_id': app_dir.name,
        'project_root': str(root),
        'sections': {},
        'excluded_by_design': [
            'secrets.env', 'social.env', 'sessions_dumps/', 'runtime_log.db',
            'llm_payload_dumps/', 'var/', 'run/', 'logs/', '*.bak*', '*.legacy',
            '*.archive', 'memory_vectors.db 原库', 'memory_layers.db 原库',
            'todo_sessions 表', 'chunks 经历相(conversation/digest 文件索引)',
        ],
    }
    mem = app_dir / 'data' / 'memories'
    vec_db = mem / 'memory_vectors.db'
    layers_db = mem / 'memory_layers.db'

    # 1. 认知相: 活跃认知, 同 cog_key 只留最新, 剥离 embedding
    n_cog = n_dedup = 0
    con = sqlite3.connect(vec_db); con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT * FROM chunks
        WHERE phase='cognition' AND supersede_by IS NULL
          AND cognition_state != 'archived'
        ORDER BY (cog_key IS NULL), created_at
    """).fetchall()
    by_key, keyless = {}, []
    for r in rows:
        d = dict(r)
        d.pop('embedding', None)
        for k, v in d.items():
            if isinstance(v, bytes):
                d[k] = v.hex()
        ck = d.get('cog_key')
        if ck:
            if ck in by_key:
                n_dedup += 1
            by_key[ck] = d
        else:
            keyless.append(d)
    kept = [*by_key.values(), *keyless]
    active_ids = {d['id'] for d in kept}
    with (out / 'cognitions.jsonl').open('w', encoding='utf-8') as f:
        for d in kept:
            f.write(json.dumps(d, ensure_ascii=False) + '\n')
            n_cog += 1
    # 2. 关联: 只导双端都活跃的边
    n_assoc = 0
    with (out / 'associations.jsonl').open('w', encoding='utf-8') as f:
        for r in con.execute('SELECT * FROM associations'):
            d = dict(r)
            if d['chunk_a'] in active_ids and d['chunk_b'] in active_ids:
                f.write(json.dumps(d, ensure_ascii=False) + '\n')
                n_assoc += 1
    con.close()
    manifest['sections']['cognitions'] = {
        'status': 'ok', 'rows': n_cog, 'dedup_same_key': n_dedup,
        'sha256': sha256_file(out / 'cognitions.jsonl')}
    manifest['sections']['associations'] = {
        'status': 'ok', 'rows': n_assoc,
        'sha256': sha256_file(out / 'associations.jsonl')}
    # 3. 分层经历
    export_db_jsonl(layers_db, "SELECT * FROM memory_layers",
                    out / 'memory_layers.jsonl', manifest, 'memory_layers')
    # 4. open todos (global)
    OPEN_STATUSES = "('planned','in_progress','paused','idea','pending','active')"
    gtodos = root / 'data' / 'global_todos.db'
    if gtodos.exists():
        con = sqlite3.connect(gtodos)
        con.row_factory = sqlite3.Row
        with (out / 'open_todos.jsonl').open('w', encoding='utf-8') as f:
            n = 0
            todos = con.execute(
                f"SELECT * FROM todos WHERE status NOT IN ('done','cancelled','completed')").fetchall()
            open_ids = [t['id'] for t in todos]
            ph = ','.join('?' * len(open_ids)) if open_ids else 'NULL'
            plans = con.execute(
                f"SELECT * FROM todo_plans WHERE task_id IN ({ph}) ORDER BY task_id, order_num",
                open_ids).fetchall() if open_ids else []
            notes = con.execute(
                f"SELECT * FROM todo_notes WHERE task_id IN ({ph}) ORDER BY task_id, id",
                open_ids).fetchall() if open_ids else []
            trigs = con.execute(
                f"SELECT * FROM todo_triggers WHERE task_id IN ({ph})",
                open_ids).fetchall() if open_ids else []
            for r in (*todos, *plans, *notes, *trigs):
                d = dict(r)
                for k, v in d.items():
                    if isinstance(v, bytes):
                        d[k] = v.hex()
                f.write(json.dumps(d, ensure_ascii=False) + '\n')
                n += 1
        con.close()
        manifest['sections']['open_todos'] = {
            'status': 'ok', 'total_lines': n, 'todos': len(todos),
            'plans': len(plans), 'notes': len(notes), 'triggers': len(trigs),
            'sha256': sha256_file(out / 'open_todos.jsonl')}

    # 5. 记忆文件 (排除备份)
    mem_exclude = re.compile(r'\.bak|\.legacy|\.archive|backup|_report\.json|\.broken')
    copy_tree_filtered(mem, out / 'memory_files', mem_exclude, manifest, 'memory_files',
                       sensitive_scan=True, exclude_under=BUNDLE_OUT)
    # 只保留 .md/.json, 其它 (db/wal/shm/report) 已由 exclude 兜底再滤一道
    removed = 0
    for junk in list((out / 'memory_files').rglob('*')):
        if junk.is_file() and junk.suffix not in ('.md', '.json', '.txt', '.csv'):
            junk.unlink()
            removed += 1
    if removed:
        manifest['sections']['memory_files']['files'] -= removed  # 修④: junk 计数回写

    # 6. persona
    copy_tree_filtered(app_dir / 'persona', out / 'persona', re.compile(r'\.bak'),
                       manifest, 'persona', sensitive_scan=True, exclude_under=BUNDLE_OUT)

    # 7. config (yaml + app.yaml 脱敏; env 一律不带)
    cfg = app_dir / 'config'
    if cfg.exists():
        cfg_dst = out / 'config'
        cfg_dst.mkdir(exist_ok=True)
        red = []
        for name in ('routines.yaml', 'subscriptions.yaml', 'app.yaml'):
            f = cfg / name
            if not f.exists():
                continue
            text = f.read_text(encoding='utf-8', errors='replace')
            lines, hit = [], False
            for ln in text.splitlines(keepends=True):
                if SENSITIVE_NAME_RE.match(ln.strip()) or SENSITIVE_VALUE_RE.search(ln):
                    lines.append(mask_line(ln)); hit = True
                else:
                    lines.append(ln)
            (cfg_dst / name).write_text(''.join(lines), encoding='utf-8')
            if hit:
                red.append(name)
        manifest['sections']['config'] = {'status': 'ok', 'redacted': red}

    # 8. skills / tools
    code_exclude = re.compile(r'__pycache__|\.pyc|\.DS_Store|node_modules|\.bak|\.legacy')
    copy_tree_filtered(app_dir / 'skills', out / 'skills', code_exclude, manifest, 'skills',
                       sensitive_scan=True, exclude_under=BUNDLE_OUT)
    copy_tree_filtered(app_dir / 'tools', out / 'tools', code_exclude, manifest, 'tools',
                       sensitive_scan=True, exclude_under=BUNDLE_OUT)

    # 9. workspace (排除临时与大目录)
    ws_exclude = re.compile(r'^(tmp|logs|cache|sessions_dumps)/|/__pycache__|\.pyc$|\.DS_Store|bundle_[^/]*|\.bak|\.legacy|trading_data/(candidate_pool_|zt_pool_|tradable_|data/)')
    copy_tree_filtered(app_dir / 'workspace', out / 'workspace', ws_exclude, manifest, 'workspace',
                       sensitive_scan=True, exclude_under=BUNDLE_OUT)

    # 修⑥(Alpha 重审残留1): manifest 计数统一以落盘真值回写, 消除 fetch 时刻漂移
    def _lc(p: Path) -> int:
        return sum(1 for _ in p.open(encoding='utf-8')) if p.exists() else 0
    for label, fn, key in (('cognitions', 'cognitions.jsonl', 'rows'),
                           ('memory_layers', 'memory_layers.jsonl', 'rows'),
                           ('open_todos', 'open_todos.jsonl', 'total_lines')):
        sec = manifest['sections'].get(label)
        if sec and sec.get('status') == 'ok':
            actual = _lc(out / fn)
            if sec.get(key) != actual:
                print(f'  [修⑥] {label}.{key}: {sec.get(key)} → {actual} (磁盘真值)')
                sec[key] = actual
    for label in ('memory_files', 'persona', 'skills', 'tools', 'workspace', 'config'):
        sec = manifest['sections'].get(label)
        if sec and sec.get('status') == 'ok' and 'files' in sec:
            actual = sum(1 for p in (out / label).rglob('*') if p.is_file()) if (out / label).exists() else 0
            if sec['files'] != actual:
                print(f'  [修⑥] {label}.files: {sec["files"]} → {actual} (磁盘真值)')
                sec['files'] = actual

    # 修⑦(Alpha 重审残留2): 剪掉空目录树 (如 _side_backup/ 0 文件)
    pruned = 0
    for d in sorted((p for p in out.rglob('*') if p.is_dir()), key=lambda x: -len(x.parts)):
        try:
            d.rmdir(); pruned += 1
        except OSError:
            pass
    if pruned:
        print(f'  [修⑦] 剪除空目录 {pruned} 个')

    manifest_path = out / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest['sections'], ensure_ascii=False, indent=2))
    print(f'\n✅ bundle → {out}')
    print(f"manifest: {manifest_path}")

if __name__ == '__main__':
    main()
