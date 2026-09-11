"""Record cache paths, sizes and times before or after a directory relocation."""
import argparse
from hashlib import sha256
import heapq
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def digest(path):
    value = sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output/'files.parquet'
    schema = pa.schema([('relative_path',pa.string()),('bytes',pa.int64()),('mtime_ns',pa.int64())])
    pending, rows, sample, count, size = [root], [], [], 0, 0
    with pq.ParquetWriter(target,schema) as writer:
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise ValueError('Nested links require separate review')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    name = str(Path(entry.path).relative_to(root))
                    rows.append(dict(relative_path=name,bytes=stat.st_size,mtime_ns=stat.st_mtime_ns))
                    count += 1; size += stat.st_size
                    rank = int(sha256(name.encode()).hexdigest(),16)
                    heapq.heappush(sample,(-rank,name))
                    if len(sample)>64: heapq.heappop(sample)
                    if len(rows)>=5000:
                        writer.write_table(pa.Table.from_pylist(rows,schema=schema));rows.clear()
        if rows: writer.write_table(pa.Table.from_pylist(rows,schema=schema))
    result = dict(root=str(root), directory_file_id=root.stat().st_ino, files=count,bytes=size,
        inventory_path=str(target.resolve()),inventory_sha256=digest(target),
        samples=[dict(relative_path=name,sha256=digest(root/name)) for _,name in sorted(sample)])
    (args.output/'summary.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='samples'},indent=2))


if __name__=='__main__': main()
