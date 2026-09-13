#!/usr/bin/env python3
"""
记忆迁移恢复脚本 v0.2 — 配套 export_memory_bundle.py
在新机器/新实例 bootstrap 完成后执行, 把 bundle 内容灌回。

用法:
  python3 restore_memory_bundle.py --app <新实例id> --bundle bundle_zero_v0.2 [--root /path/to/数字生命]

行为:
  1. 文件资产: memory_files/→data/memories/, persona/, config/(仅 yaml, 不动 secrets),
     skills/, tools/, workspace/ — 全部覆盖同名(旧内容先备份到 bundle_side_backup/)
  2. 认知相: cog_key 查重(跳过已有), chunk_hash 冲突重hash, embedding 置 NULL
     (bootstrap 的 ensure_indexed 会在新机 embedding 可用后自动补算/复用)
  3. associations: 老id→新id 映射, 两端都映射上才迁移
  4. memory_layers: 全表灌入(同构表)
  5. open_todos: 按 jsonl 字段特征分流回 global_todos 的 4 张表, id 重映射
打印 restore_report.json 汇总。

注意: 本脚本不动新实例的 secrets.env/social.env/app.yaml(含端点凭据) — 差异人工 review。
"""
import argparse, hashlib, json, re, shutil, sqlite3, sys, time, tempfile
from pathlib import Path

def find_project_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / 'data' / 'global_todos.db').exists():
            return p
    raise SystemExit('找不到项目根')

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', required=True, help='新实例短 id (须已 bootstrap)')
    ap.add_argument('--bundle', required=True, help='bundle 目录')
    ap.add_argument('--root', default=None)
    ap.add_argument('--backup-dir', default=None, help='被覆盖文件备份目录(默认/tmp,绝不进bundle)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    bundle = Path(args.bundle).resolve()
    if not (bundle / 'manifest.json').exists():
        raise SystemExit(f'{bundle} 不是有效 bundle (缺 manifest.json)')
    script_dir = Path(__file__).resolve().parent
    root = Path(args.root) if args.root else find_project_root(script_dir)
    apps = list((root / 'apps').glob(f'{args.app}*'))
    if not apps:
        raise SystemExit(f'apps/ 下找不到新实例 {args.app}* (先跑 bootstrap)')
    app_dir = apps[0]
    mem = app_dir / 'data' / 'memories'
    mem.mkdir(parents=True, exist_ok=True)

    report = {'restored_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'bundle': str(bundle), 'app': app_dir.name, 'dry_run': args.dry_run,
              'files': {}, 'db': {}}
    # 修⑭: 备份目录绝不放 bundle 内部（restore 副作用会污染源包）
    backup_dir = Path(args.backup_dir) if getattr(args, 'backup_dir', None) else Path(tempfile.gettempdir()) / ('mm_side_backup_' + time.strftime('%H%M%S'))
    backup_dir.mkdir(parents=True, exist_ok=True)
    print(f'[修⑭] side_backup → {backup_dir} (bundle 外)')

    def copy_overwrite(src: Path, dst: Path, label: str):
        n = 0
        for f in sorted(src.rglob('*')):
            if f.is_dir() or '__pycache__' in f.parts or f.suffix == '.pyc':
                continue
            rel = f.relative_to(src)
            target = dst / rel
            if target.exists():
                bak = backup_dir / label / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                if not args.dry_run:
                    shutil.copy2(target, bak)
            if not args.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
            n += 1
        report['files'][label] = n

    # 1. 文件资产
    if (bundle / 'memory_files').exists():
        copy_overwrite(bundle / 'memory_files', mem, 'memory_files')
    for sec in ('persona', 'skills', 'tools', 'workspace'):
        if (bundle / sec).exists():
            copy_overwrite(bundle / sec, app_dir / sec, sec)
    # config: 只 yaml, 不碰 env; app.yaml 若新机已有 → 跳过并提示 diff
    cfg_src, cfg_dst = bundle / 'config', app_dir / 'config'
    if cfg_src.exists():
        n, skipped = 0, []
        for f in cfg_src.glob('*.yaml'):
            target = cfg_dst / f.name
            if f.name == 'app.yaml' and target.exists():
                skipped.append(f.name + '(新机已有,人工diff)')
                continue
            if not args.dry_run:
                cfg_dst.mkdir(exist_ok=True)
                if target.exists():
                    bak = backup_dir / 'config' / f.name
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, bak)
                shutil.copy2(f, target)
            n += 1
        report['files']['config'] = f'{n} (跳过: {skipped})' if skipped else n

    # 2. 认知相
    vec_db = mem / 'memory_vectors.db'
    cog_file = bundle / 'cognitions.jsonl'
    id_map = {}
    if cog_file.exists() and vec_db.exists():
        con = sqlite3.connect(vec_db)
        con.row_factory = sqlite3.Row
        cols = [c[1] for c in con.execute('PRAGMA table_info(chunks)').fetchall()]
        existing_keys = {r[0] for r in con.execute(
            "SELECT DISTINCT cog_key FROM chunks WHERE cog_key IS NOT NULL")}
        existing_hash_body = {r[0]: r[1] for r in con.execute(
            'SELECT chunk_hash, text FROM chunks')}
        inserted = skipped_key = rehash = 0
        with cog_file.open(encoding='utf-8') as f:
            for line in f:
                d = json.loads(line)
                old_id = d['id']
                ck = d.get('cog_key')
                if ck and ck in existing_keys:
                    skipped_key += 1
                    id_map[old_id] = None  # 已有同 key, 老id作废
                    continue
                h = d['chunk_hash']
                if h in existing_hash_body:
                    if existing_hash_body[h] == d.get('text'):
                        skipped_key += 1  # 无cog_key但内容完全一致: 视为已有, 幂等跳过
                        id_map[old_id] = None
                        continue
                    h = 'mig:' + sha256_text(h + str(old_id))[:16]
                    rehash += 1
                d['chunk_hash'] = h
                d['embedding'] = None
                d = {k: d.get(k) for k in cols if k != 'id'}
                ph = ','.join('?' * len(d))
                if not args.dry_run:
                    cur = con.execute(
                        f'INSERT INTO chunks ({",".join(d)}) VALUES ({ph})',
                        list(d.values()))
                    id_map[old_id] = cur.lastrowid
                else:
                    id_map[old_id] = -1
                existing_keys.add(ck) if ck else None
                existing_hash_body[h] = d.get('text')
                inserted += 1
        if not args.dry_run:
            con.commit()
        con.close()
        report['db']['cognitions'] = {'inserted': inserted, 'skipped_same_key': skipped_key,
                                      'rehashed': rehash}

    # 3. associations
    assoc_file = bundle / 'associations.jsonl'
    if assoc_file.exists() and id_map:
        con = sqlite3.connect(vec_db)
        kept = dropped = 0
        with assoc_file.open(encoding='utf-8') as f:
            for line in f:
                d = json.loads(line)
                a, b = id_map.get(d['chunk_a']), id_map.get(d['chunk_b'])
                if a and b:
                    if not args.dry_run:
                        con.execute(
                            'INSERT INTO associations (chunk_a, chunk_b, weight, last_activated)'
                            ' VALUES (?,?,?,?)', (a, b, d.get('weight'), d.get('last_activated')))
                    kept += 1
                else:
                    dropped += 1
        if not args.dry_run:
            con.commit()
        con.close()
        report['db']['associations'] = {'kept': kept, 'dropped': dropped}

    # 4. memory_layers
    ml_file = bundle / 'memory_layers.jsonl'
    layers_db = mem / 'memory_layers.db'
    if ml_file.exists() and layers_db.exists():
        con = sqlite3.connect(layers_db)
        cols = [c[1] for c in con.execute('PRAGMA table_info(memory_layers)').fetchall()]
        before = con.execute('SELECT COUNT(*) FROM memory_layers').fetchone()[0]
        # 幂等键: (layer, period, start_time, end_time) — clone 场景旧库已有同经历则跳过
        seen = {tuple(str(x) for x in r) for r in con.execute(
            'SELECT layer, period, start_time, end_time FROM memory_layers')}
        n = skipped_ml = 0
        with ml_file.open(encoding='utf-8') as f:
            for line in f:
                d = json.loads(line)
                k3 = (str(d.get('layer')), str(d.get('period')),
                      str(d.get('start_time')), str(d.get('end_time')))
                if k3 in seen:
                    skipped_ml += 1
                    continue
                d = {k: d.get(k) for k in cols if k != 'id'}
                ph = ','.join('?' * len(d))
                if not args.dry_run:
                    con.execute(f'INSERT INTO memory_layers ({",".join(d)}) VALUES ({ph})',
                                list(d.values()))
                seen.add(k3)
                n += 1
        if not args.dry_run:
            con.commit()
        con.close()
        report['db']['memory_layers'] = {'inserted': n, 'skipped_dup': skipped_ml,
                                         'table_before': before}

    # 5. open_todos → global_todos.db
    ot_file = bundle / 'open_todos.jsonl'
    gtodos = root / 'data' / 'global_todos.db'
    if ot_file.exists() and gtodos.exists():
        con = sqlite3.connect(gtodos)
        con.row_factory = sqlite3.Row
        by_type = {'todo': [], 'plan': [], 'note': [], 'trigger': []}
        with ot_file.open(encoding='utf-8') as f:
            for line in f:
                d = json.loads(line)
                if 'title' in d:
                    by_type['todo'].append(d)
                elif 'order_num' in d:
                    by_type['plan'].append(d)
                elif 'trigger_type' in d:
                    by_type['trigger'].append(d)
                else:
                    by_type['note'].append(d)
        todo_id_map = {}
        counts = {}
        tcols = [c[1] for c in con.execute('PRAGMA table_info(todos)').fetchall()]
        existing_todo_sig = {tuple(r) for r in con.execute(
            'SELECT title, created_at FROM todos')}
        skipped_todos = 0
        for d in by_type['todo']:
            old = d['id']
            if (d.get('title'), d.get('created_at')) in existing_todo_sig:
                todo_id_map[old] = None  # 已存在(同标题+创建时间), 跳过并断开子表
                skipped_todos += 1
                continue
            dd = {k: d.get(k) for k in tcols if k != 'id'}
            ph = ','.join('?' * len(dd))
            if not args.dry_run:
                cur = con.execute(f'INSERT INTO todos ({",".join(dd)}) VALUES ({ph})',
                                  list(dd.values()))
                todo_id_map[old] = cur.lastrowid
            else:
                todo_id_map[old] = -1
        counts['todos'] = len(by_type['todo']) - skipped_todos
        counts['todos_skipped_dup'] = skipped_todos
        pcols = [c[1] for c in con.execute('PRAGMA table_info(todo_plans)').fetchall()]
        for d in by_type['plan']:
            new = todo_id_map.get(d['task_id'])
            if not new:
                continue
            dd = {k: d.get(k) for k in pcols if k != 'id'}
            dd['task_id'] = new
            ph = ','.join('?' * len(dd))
            if not args.dry_run:
                con.execute(f'INSERT INTO todo_plans ({",".join(dd)}) VALUES ({ph})',
                            list(dd.values()))
        counts['plans'] = sum(1 for d in by_type['plan'] if todo_id_map.get(d['task_id']))
        ncols = [c[1] for c in con.execute('PRAGMA table_info(todo_notes)').fetchall()]
        for d in by_type['note']:
            new = todo_id_map.get(d['task_id'])
            if not new:
                continue
            dd = {k: d.get(k) for k in ncols if k != 'id'}
            dd['task_id'] = new
            ph = ','.join('?' * len(dd))
            if not args.dry_run:
                con.execute(f'INSERT INTO todo_notes ({",".join(dd)}) VALUES ({ph})',
                            list(dd.values()))
        counts['notes'] = sum(1 for d in by_type['note'] if todo_id_map.get(d['task_id']))
        trcols = [c[1] for c in con.execute('PRAGMA table_info(todo_triggers)').fetchall()]
        for d in by_type['trigger']:
            new = todo_id_map.get(d['task_id'])
            if not new:
                continue
            dd = {k: d.get(k) for k in trcols if k != 'id'}
            dd['task_id'] = new
            ph = ','.join('?' * len(dd))
            if not args.dry_run:
                con.execute(f'INSERT INTO todo_triggers ({",".join(dd)}) VALUES ({ph})',
                            list(dd.values()))
        counts['triggers'] = sum(1 for d in by_type['trigger'] if todo_id_map.get(d['task_id']))
        if not args.dry_run:
            con.commit()
        con.close()
        report['db']['open_todos'] = counts

    out = Path('restore_report.json').resolve()
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'\n{"🔍 dry-run" if args.dry_run else "✅ 完成"} — report: {out}')
    print('后续手工步骤: ① diff 新旧 config/app.yaml ② 新机 embedding 可用后'
          ' ensure_indexed 自动补算向量 ③ 抽查 recall 验证')

if __name__ == '__main__':
    main()
