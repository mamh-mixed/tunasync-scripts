#!/usr/bin/env python3
import os
import sys
import threading
import traceback
import queue
from pathlib import Path
from datetime import datetime
import tempfile
import hashlib

import requests


BASE_URL = os.getenv("TUNASYNC_UPSTREAM_URL", "https://api.github.com/repos/")
WORKING_DIR = os.getenv("TUNASYNC_WORKING_DIR")
REPOS = [
    # owner/repo, tree, tree, tree, blob
    ["fpco/minghc", "master", "bin", "7z.exe"],
    ["fpco/minghc", "master", "bin", "7z.dll"],
]

# connect and read timeout value
TIMEOUT_OPTION = (7, 10)
total_size = 0

# wrap around requests.get to use token if available
def github_get(*args, **kwargs):
    headers = kwargs['headers'] if 'headers' in kwargs else {}
    if 'GITHUB_TOKEN' in os.environ:
        headers['Authorization'] = 'token {}'.format(
            os.environ['GITHUB_TOKEN'])
    kwargs['headers'] = headers
    return requests.get(*args, **kwargs)

def github_tree(*args, **kwargs):
    headers = kwargs['headers'] if 'headers' in kwargs else {}
    headers["Accept"] = "application/vnd.github.v3+json"
    kwargs['headers'] = headers
    return github_get(*args, **kwargs)

# NOTE blob API supports file up to 100MB
# To get larger one, we need raw.githubcontent, which is not implemented now
def github_blob(*args, **kwargs):
    headers = kwargs['headers'] if 'headers' in kwargs else {}
    headers["Accept"] = "application/vnd.github.v3.raw"
    kwargs['headers'] = headers
    return github_get(*args, **kwargs)

def do_download(remote_url: str, dst_file: Path, remote_size: int, sha: str):
    # NOTE the stream=True parameter below
    with github_blob(remote_url, stream=True) as r:
        r.raise_for_status()
        tmp_dst_file = None
        try:
            with tempfile.NamedTemporaryFile(prefix="." + dst_file.name + ".", suffix=".tmp", dir=dst_file.parent, delete=False) as f:
                tmp_dst_file = Path(f.name)
                for chunk in r.iter_content(chunk_size=1024**2):
                    if chunk:  # filter out keep-alive new chunks
                        f.write(chunk)
                        # f.flush()
            # check for downloaded size
            downloaded_size = tmp_dst_file.stat().st_size
            if remote_size != -1 and downloaded_size != remote_size:
                raise Exception(f'File {dst_file.as_posix()} size mismatch: downloaded {downloaded_size} bytes, expected {remote_size} bytes')
            tmp_dst_file.chmod(0o644)
            target = dst_file.parent / ".sha" / sha
            print("symlink", dst_file)
            print("target", target)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_dst_file.replace(target)
            if dst_file.is_symlink():
                origin = dst_file.parent / os.readlink(dst_file)
                print("origin", origin)
                dst_file.unlink()
                origin.unlink()
            dst_file.symlink_to(Path(".sha") / sha)
        finally:
            if not tmp_dst_file is None:
                if tmp_dst_file.is_file():
                    tmp_dst_file.unlink()

def downloading_worker(q):
    while True:
        item = q.get()
        if item is None:
            break

        dst_file = Path('/'.join(item))
        dst_file.parent.mkdir(parents=True, exist_ok=True)

        item.pop(0) # remove working dir
        owner_repo = item.pop(0)
        try:
            tree = item.pop(0)
            tree_child = item.pop(0)
            child_is_leaf = False
            url = ''
            sha = ''
            size = 0
            while not child_is_leaf:
                with github_tree(f"{BASE_URL}{owner_repo}/git/trees/{tree}") as r:
                    r.raise_for_status()
                    tree_json = r.json()
                    for child in tree_json["tree"]:
                        if tree_child == child["path"]:
                            if child["type"] == "tree":
                                tree = child["sha"]
                                tree_child = item.pop(0)
                            elif child["type"] == "blob":
                                child_is_leaf = True
                                url = child["url"]
                                size = child["size"]
                                sha = child["sha"]
                            else:
                                raise Exception
                            break
                    else:
                        raise Exception
            if not dst_file.is_symlink() or \
                dst_file.stat().st_size != size or \
                Path(os.readlink(dst_file)).name != sha:
                do_download(url, dst_file, size, sha)
            else:
                print("Skip", dst_file)
        except Exception as e:
            print(e)
            print("Failed to download", dst_file, flush=True)
            if dst_file.is_file():
                dst_file.unlink()

        q.task_done()


def create_workers(n):
    task_queue = queue.Queue()
    for i in range(n):
        t = threading.Thread(target=downloading_worker, args=(task_queue, ))
        t.start()
    return task_queue

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", default=WORKING_DIR)
    parser.add_argument("--workers", default=1, type=int,
                        help='number of concurrent downloading jobs')
    args = parser.parse_args()

    if args.working_dir is None:
        raise Exception("Working Directory is None")

    working_dir = args.working_dir
    task_queue = create_workers(args.workers)

    for cfg in REPOS:
        cfg.insert(0, working_dir)
        task_queue.put(cfg)

    # block until all tasks are done
    task_queue.join()
    # stop workers
    for i in range(args.workers):
        task_queue.put(None)

if __name__ == "__main__":
    main()

# vim: ts=4 sw=4 sts=4 expandtab
