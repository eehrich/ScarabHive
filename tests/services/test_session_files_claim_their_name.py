"""A handed-over file claims its name by creating it: two hand-overs of one name never share a file."""
from __future__ import annotations

import threading

from agent_system.services import session_files


def test_hand_overs_of_one_name_at_once_each_get_their_own_file(tmp_path):
    root = tmp_path / "session_files"
    sources = []
    for n in range(8):
        source = tmp_path / f"src{n}" / "bild.png"
        source.parent.mkdir()
        source.write_bytes(f"picture {n}".encode())
        sources.append(source)

    start = threading.Barrier(len(sources))
    results: list = [None] * len(sources)

    def hand_over(n: int) -> None:
        start.wait()
        results[n] = session_files.hand_over(root, "u", "s1", sources[n])

    threads = [threading.Thread(target=hand_over, args=(n,)) for n in range(len(sources))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    paths = [path for _url, path in results]
    assert len(set(paths)) == len(sources)
    assert sorted(path.read_bytes() for path in paths) == sorted(s.read_bytes() for s in sources)


def test_a_taken_name_gets_a_number_and_the_first_stays(tmp_path):
    root = tmp_path / "session_files"
    first = tmp_path / "a" / "bg.png"
    second = tmp_path / "b" / "bg.png"
    for path, body in ((first, b"one"), (second, b"two")):
        path.parent.mkdir()
        path.write_bytes(body)

    url1, path1 = session_files.hand_over(root, "u", "s1", first)
    url2, path2 = session_files.hand_over(root, "u", "s1", second)

    assert (path1.name, path2.name) == ("bg.png", "bg-2.png")
    assert url2.endswith("/files/bg-2.png")
    assert path1.read_bytes() == b"one" and path2.read_bytes() == b"two"


def test_a_missing_source_leaves_no_empty_file(tmp_path):
    root = tmp_path / "session_files"
    try:
        session_files.hand_over(root, "u", "s1", tmp_path / "gone.png")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("a missing source was handed over")
    assert list(session_files.files_dir(root, "u", "s1").iterdir()) == []
