from greet import greet


def test_greets_by_name():
    assert greet("Ada") == "Hello, Ada!"


def test_greets_another_name():
    assert greet("Grace") == "Hello, Grace!"
