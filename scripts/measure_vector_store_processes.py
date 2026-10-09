"""Do documents survive several processes writing one chroma store at once?

The evidence behind the cross-process guards in
``agent_system/utils/vector_store/chroma.py`` (_CHROMA_INIT_LOCK, _CHROMA_WRITE_LOCK,
_ChromaAccess). It is a MEASUREMENT, not a unit test: the loss is a race that
needs 16 writers x 200 documents to show reliably -- minutes per run, and at
test sizes it simply does not happen, with or without the guards (tried with
a start barrier too). Run it again after a chromadb upgrade. Windows and Linux
are two different engines: VectorStore uses chroma's Python SegmentAPI on
Windows and its Rust bindings everywhere else.

    python scripts/measure_vector_store_processes.py 16 200 --warm            # the guards as they are
    python scripts/measure_vector_store_processes.py 16 200 --warm --serial   # control: one after another
    python scripts/measure_vector_store_processes.py 16 200 --warm --nofix    # no write guard
    python scripts/measure_vector_store_processes.py 16 200 --warm --lockonly # the lock without the reopen
    python scripts/measure_vector_store_processes.py 16 200 --warm --twin --per-instance
                                                  # a second store on the path, one access each

Each writer adds its documents in batches of 10 (vectors from one seeded
generator per writer); a fresh process then asks for every fifth document by
its own vector. "IN_TOP10" separates the two reasons for a miss: HNSW is
approximate and may rank a present vector second, a lost one is nowhere.
The guards are switched off IN MEMORY of the writer processes only.

Measured 21.09.2026 on Windows, chromadb 1.5.9, 16 x 200, found in the top 10 of 640:

    one after another (control)            638, 638, 638
    no guard                               596, 598, 639, 637   -- a race: sometimes
    the lock alone                         200, 444             -- worse than nothing
    lock + reopen (as built)               635, 636, 637, 639, 639, 639, 635
    + a second store per process, shared   637, 639
    + a second store per process, one access each   638, 206

The as-built runs scatter above and below the control, so the rest is the
order HNSW happened to insert in, not loss. chromadb 1.4.0 lost more: 200-280
of 320 already with 8 writers.

Linux (WSL Ubuntu, Rust bindings), same day, same sizes:

    one after another (control)            636
    no guard                               reader HUNG in count(), both runs
    the lock alone                         405, 246
    lock + reopen (as built)               638, 637, 637
    + a second store per process, shared   639
    + a second store per process, one access each   244

The hang is the store, not the reader: a fresh process on a copy of it hangs
in count() too. A reader that outlives READER_TIMEOUT is reported as HUNG.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
FLAGS = ("--nofix", "--lockonly", "--twin", "--per-instance")
PASSED = [flag for flag in FLAGS if flag in sys.argv]
READER_TIMEOUT = 300  # a sound store answers 640 queries in well under a minute

WRITER = r"""
import random, sys
sys.path.insert(0, sys.argv[4])
from agent_system.utils.vector_store import VectorStore, chroma
from agent_system.utils.vector_store import store as vs_store
if "--nofix" in sys.argv:
    import contextlib
    chroma.ChromaBackend._write = lambda self: contextlib.nullcontext()
if "--lockonly" in sys.argv:
    from filelock import FileLock
    chroma.ChromaBackend._write = lambda self: FileLock(str(self.persist_path / chroma._CHROMA_WRITE_LOCK))
if "--per-instance" in sys.argv:
    def _own(persist_path):
        access = chroma._ChromaAccess()
        access.users = 1
        return access, str(id(access))
    vs_store._acquire_access = _own
path, tag, k = sys.argv[1], sys.argv[2], int(sys.argv[3])
twin = None
if "--twin" in sys.argv:
    twin = VectorStore(persist_path=path)
    twin.count("shared")
store = VectorStore(persist_path=path)
rnd = random.Random(tag)
for start in range(0, k, 10):
    ids = [f"{tag}_{i}" for i in range(start, min(start + 10, k))]
    store.add("shared", ids=ids, documents=ids,
              embeddings=[[rnd.random() for _ in range(384)] for _ in ids])
store.close()
if twin is not None:
    twin.close()
"""

READER = r"""
import random, sys
sys.path.insert(0, sys.argv[4])
from agent_system.utils.vector_store import VectorStore
store = VectorStore(persist_path=sys.argv[1])
n, k = int(sys.argv[2]), int(sys.argv[3])
print("COUNT", store.count("shared"))
found = present = asked = 0
for w in range(n):
    tag = f"p{w}"
    rnd = random.Random(tag)
    for start in range(0, k, 10):
        ids = [f"{tag}_{i}" for i in range(start, min(start + 10, k))]
        vecs = [[rnd.random() for _ in range(384)] for _ in ids]
        for doc_id, vec in list(zip(ids, vecs))[::5]:
            got = (store.query("shared", query_embedding=vec, n_results=10).get("ids") or [[]])[0] or []
            asked += 1
            found += 1 if got and got[0] == doc_id else 0
            present += 1 if doc_id in got else 0
print("RECALL", found, "of", asked, "| IN_TOP10", present, "of", asked)
store.close()
"""


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    path = Path(tempfile.mkdtemp()) / "vectors"

    def writer(tag: str, count: int, flags: list) -> list:
        return [sys.executable, "-c", WRITER, str(path), tag, str(count), SRC, *flags]

    if "--warm" in sys.argv:          # the store and the collection exist, as after the first run of a day
        subprocess.run(writer("seed", 1, []), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    died = []
    if "--serial" in sys.argv:
        for w in range(n):
            subprocess.run(writer(f"p{w}", k, PASSED), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        procs = [subprocess.Popen(writer(f"p{w}", k, PASSED), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True) for w in range(n)]
        for proc in procs:
            err = proc.communicate(timeout=1800)[1]
            if proc.returncode:
                lines = [line for line in err.splitlines() if line.strip()]
                died.append(lines[-1][:200] if lines else "?")
    print(f"writers: {n} x {k} {' '.join(PASSED) or '(as built)'}   died: {len(died)} {died[:2]}")
    try:
        out = subprocess.run([sys.executable, "-c", READER, str(path), str(n), str(k), SRC],
                             capture_output=True, text=True, timeout=READER_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"reader: HUNG (no answer in {READER_TIMEOUT} s) -- store kept at {path}")
        return
    print("reader:", [line for line in out.stdout.splitlines()
                      if line.startswith(("COUNT", "RECALL"))] or out.stderr[-300:])


if __name__ == "__main__":
    main()
