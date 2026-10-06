"""The Help panel's library: the AmigaGuide reader, where the guides come from, and the routes serving them."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.ui.amigaguide import layout, parse, plain_text
from agent_system.ui.help import Library
from agent_system.ui.routes import router

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIRS = sorted(d for d in (REPO / "src").glob("plugins*") if d.is_dir())


def laid_out(source, resolve=lambda target: ("g", target.lower())):
    guide = parse(source, "g")
    node = next(iter(guide.nodes.values()))
    return layout(node, guide, resolve)


def texts(result):
    return ["".join(span["text"] for span in line["spans"]) for line in result["lines"]]


def links(result):
    """Every link button of a laid-out node, table cells included."""
    spans = [span for line in result["lines"] for span in line["spans"]]
    spans += [span for line in result["lines"] if line.get("kind") == "table"
              for row in line["rows"] for cell in row for span in cell]
    return [span["link"] for span in spans if "link" in span]


def markup(result):
    """The rendered Markdown of a laid-out node, all blocks together."""
    return "".join(line.get("html", "") for line in result["lines"])


# --- the reader ------------------------------------------------------------

def test_smartwrap_joins_a_paragraph_and_code_keeps_its_lines():
    # an indented @{body} is a command, not a line of blanks at the end of the code
    result = laid_out("@smartwrap\n@node main\nOne\n  two\n\n@{code}\n  a\n  b\n  @{body}\nthree\n@endnode")

    assert texts(result) == ["One two", "", "  a", "  b", "three"]
    assert [line["wrap"] for line in result["lines"]] == [True, True, False, False, True]


@pytest.mark.parametrize("setting, wrap", [("", False), ("@wordwrap\n", True)])
def test_without_smartwrap_every_line_stays_a_line(setting, wrap):
    result = laid_out(f"{setting}@node main\nOne\ntwo\n@endnode")

    assert texts(result) == ["One", "two"]
    assert all(line["wrap"] is wrap for line in result["lines"])


def test_alignment_holds_for_the_lines_it_starts_not_the_paragraph_before():
    result = laid_out("@smartwrap\n@node main\n@{jcenter}\n@{b}Title@{ub}\n@{jleft}\n\nBody\n@endnode")

    assert [(line["align"], texts({"lines": [line]})[0]) for line in result["lines"]] == [
        ("center", "Title"), ("left", ""), ("left", "Body")]


def test_attributes_become_classes_and_escapes_stay_text():
    result = laid_out("@node main\n@{b}bold@{ub} \\@{b} \\\\ @{fg shine}x@{fg text}\n@endnode")

    assert result["lines"][0]["spans"] == [
        {"text": "bold", "style": ["b"]},
        {"text": " @{b} \\ ", "style": []},
        {"text": "x", "style": ["fg-shine"]},
    ]


def test_a_link_leads_to_its_node_a_dead_one_and_a_command_do_not():
    targets = {"there": ("g", "there")}
    result = laid_out('@node main\n@{" Go " link There 5} @{" Dead " link nowhere} @{" Run " system rm -rf /}\n@endnode',
                      resolve=lambda target: targets.get(target.lower()))

    buttons = [span for span in result["lines"][0]["spans"] if len(span) > 2]
    assert buttons[0] == {"text": " Go ", "link": {"guide": "g", "node": "there", "line": 5}, "style": []}
    assert buttons[1]["broken"] == "nowhere" and buttons[2]["inert"] == "system"
    assert result["problems"] == ["line 2: link to 'nowhere' leads nowhere"]


def test_a_web_link_goes_out_and_javascript_stays_a_node_name():
    result = laid_out('@node main\n@{" Site " link https://example.org/a?b=1} @{" Mail " link mailto:x@example.org} '
                      '@{" Evil " link javascript:alert(1)}\n@endnode', resolve=lambda target: None)

    buttons = [span for span in result["lines"][0]["spans"] if len(span) > 2]
    assert [button.get("url") for button in buttons] == ["https://example.org/a?b=1", "mailto:x@example.org", None]
    assert buttons[2]["broken"] == "javascript:alert(1)"


def test_an_image_in_a_paragraph_follows_a_blank_like_a_word():
    guide = parse('@smartwrap\n@node main\nSee\n@{image pic.png "P"}\n@endnode', "g")

    result = layout(guide.nodes["main"], guide, lambda target: None, lambda path: f"/img/{path}")

    assert result["lines"][0]["spans"] == [{"text": "See ", "style": []},
                                           {"text": "P", "image": "/img/pic.png", "style": []}]


def test_structural_mistakes_are_reported_not_swallowed():
    guide = parse("@node a\n@bogus\n@endnode\n@node A\n@endnode\n@node b\n", "g")

    assert guide.warnings == [
        "line 2: unknown command @bogus",
        "line 4: node 'A' defined twice; the first one counts",
        "end of file: @endnode missing for 'b'",
    ]


def test_a_guide_without_an_index_gets_one_listing_its_nodes_by_title():
    guide = parse('@node main "Zeta"\n@endnode\n@node other "Alpha"\n@endnode', "g")

    assert guide.index == "index" and guide.nodes["index"].generated
    result = layout(guide.nodes["index"], guide, lambda target: ("g", target.lower()))
    assert [link["node"] for link in links(result)] == ["other", "main"]


# --- the library -----------------------------------------------------------

@pytest.fixture
def plugins(tmp_path):
    root = tmp_path / "plugins"

    def plugin(name, guide=None, readme=None):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "plugin.toml").write_text(f'[plugin]\nname = "{name}"\ndescription = "Probe {name}"\n',
                                            encoding="utf-8")
        if guide is not None:
            (folder / f"{name}.guide").write_text(guide, encoding="utf-8")
        if readme is not None:
            (folder / "README.md").write_text(readme, encoding="utf-8")
        return folder

    return root, plugin


def test_the_plugin_list_links_a_guide_or_else_the_readme(plugins):
    root, plugin = plugins
    plugin("guided", guide='@node main "Guided"\nSee @{" manual " link scarabhive/main}\n@endnode')
    plugin("readme_only", readme="# Readme\n@decorator\n")
    plugin("bare")
    library = Library([root])

    index = library.page("plugins", "main")
    assert {link["guide"] for link in links(index)} == {"guided", "readme_only"}
    assert "bare |  | Probe bare" in plain_text(index)  # listed, without a link
    piped = plugin("piped")
    (piped / "plugin.toml").write_text('[plugin]\nname = "piped"\ndescription = "reads | writes"\n', encoding="utf-8")
    table = next(line for line in Library([root]).page("plugins", "main")["lines"] if line.get("kind") == "table")
    row = next(row for row in table["rows"] if texts({"lines": [{"spans": row[0]}]})[0].strip() == "piped")
    assert len(row) == 3  # the | in the description stays in its cell
    assert index["problems"] == []
    readme = library.page("readme_only", "main")
    assert "<h1>Readme</h1>" in markup(readme)  # a README is Markdown and shown as such
    assert "@decorator" in markup(readme)  # README text is text, even where it looks like a command
    assert readme["nav"]["contents"] == {"guide": "plugins", "node": "main"}
    assert library.page("guided", "main")["problems"] == []


@pytest.mark.parametrize("target", ["guided/main", "guided.guide/Main", "HELP:guided.guide/main", "../x/guided.guide/main"])
def test_a_link_names_another_guide_by_its_id_or_its_file(plugins, target):
    root, plugin = plugins
    plugin("guided", guide='@node main "Guided"\n@endnode')

    assert Library([root]).resolve("scarabhive", target) == ("guided", "main")


def test_browse_follows_the_file_and_help_falls_back_to_the_manual(plugins):
    root, plugin = plugins
    plugin("guided", guide='@node main "M"\n@endnode\n@node two "T"\n@endnode\n@node three "3"\n@prev main\n@endnode')
    library = Library([root])

    assert library.page("guided", "two")["nav"] == {
        "contents": {"guide": "guided", "node": "main"},
        "index": {"guide": "guided", "node": "index"},
        "help": {"guide": "scarabhive", "node": "help"},
        "prev": {"guide": "guided", "node": "main"},
        "next": {"guide": "guided", "node": "three"},
    }
    last = library.page("guided", "three")["nav"]
    assert last["prev"] == {"guide": "guided", "node": "main"}  # @prev wins over the file order
    assert last["next"] is None  # the generated index is not a page to browse into


def test_a_plugin_named_like_a_manual_guide_does_not_replace_it(plugins):
    root, plugin = plugins
    plugin("scarabhive", guide='@node main "Impostor"\n@endnode')
    plugin("plugins", readme="impostor")
    library = Library([root])

    assert "quickstart" in library.guides["scarabhive"].nodes
    assert library.page("plugins", "main")["title"] == "Plugins"


def test_search_finds_nodes_with_every_word_title_hits_first(plugins):
    root, plugin = plugins
    plugin("guided", guide='@node main "Main"\nzebra quokka\n@endnode\n@node q "Quokka zebra"\nnothing\n@endnode')
    library = Library([root])

    assert [hit["node"] for hit in library.search("Zebra quokka") if hit["guide"] == "guided"] == ["q", "main"]
    assert library.search("zebra xylophonequokka") == []


def test_an_edited_guide_shows_on_the_next_request(plugins):
    root, plugin = plugins
    folder = plugin("guided", guide='@node main "Old"\n@endnode')
    assert Library([root]).page("guided", "main")["title"] == "Old"

    (folder / "guided.guide").write_text('@node main "Newer"\n@endnode', encoding="utf-8")

    assert Library([root]).page("guided", "main")["title"] == "Newer"


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def test_an_image_next_to_the_guide_is_shown_and_one_elsewhere_is_not(plugins):
    root, plugin = plugins
    folder = plugin("guided", guide='@node main "G"\n@{image pic.png "A picture"}\n@{image ../secret.png}\n'
                                    '@{image notes.txt}\n@endnode')
    (folder / "pic.png").write_bytes(PNG)
    (root / "secret.png").write_bytes(PNG)
    (folder / "notes.txt").write_text("not an image", encoding="utf-8")

    page = Library([root]).page("guided", "main")

    spans = [span for line in page["lines"] for span in line["spans"]]
    assert spans[0] == {"text": "A picture", "image": "/api/help/asset?guide=guided&path=pic.png", "style": []}
    assert [span.get("broken") for span in spans[1:]] == ["../secret.png", "notes.txt"]
    assert len(page["problems"]) == 2


def test_embed_shows_a_file_as_written_and_follows_its_changes(plugins, tmp_path):
    root, plugin = plugins
    folder = plugin("guided", guide='@smartwrap\n@node main "G"\nBefore\n@embed example.yaml\n'
                                    '@embed ../../outside.txt\n@endnode')
    (folder / "example.yaml").write_text("key: 1\npath: C:\\\\dir @{b}\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    library = Library([root])

    page = library.page("guided", "main")
    assert texts(page)[0] == "Before"
    # a code block in the file's language, every character as written -- guide escapes mean nothing in it
    block = next(line for line in page["lines"] if line.get("kind") == "code")
    assert block["language"] == "yaml"
    assert [texts({"lines": [{"spans": row}]})[0] for row in block["rows"]] == ["key: 1", "path: C:\\\\dir @{b}"]
    assert library.guides["guided"].warnings == [
        "line 5: @embed needs a node and a readable file next to the guide, got ['../../outside.txt']"]

    (folder / "example.yaml").write_text("key: 22\n", encoding="utf-8")

    assert "key: 22" in plain_text(Library([root]).page("guided", "main"))


def kinds(result):
    """(kind, marker, text) of every line that has a kind -- the blocks of the extended format."""
    return [(line["kind"], line.get("marker"), texts({"lines": [line]})[0])
            for line in result["lines"] if "kind" in line]


def test_headings_lists_quotes_and_rules_are_paragraphs_of_their_own():
    result = laid_out("@smartwrap\n@node main\n@{h1}Title\nIntro\n@{bullet}one\nstill one\n@{bullet 2}nested\n"
                      "@{number}first\n@{number}second\n\n@{number}third, after a blank line\n\nPlain.\n\n"
                      "@{number}first again\n@{quote}Said.\n@{rule}\n@{h2}Next\n@endnode")

    assert kinds(result) == [
        ("h1", None, "Title"), ("bullet", "•", "one still one"), ("bullet", "◦", "nested"),
        ("number", "1.", "first"), ("number", "2.", "second"), ("number", "3.", "third, after a blank line"),
        ("number", "1.", "first again"), ("quote", None, "Said."), ("rule", None, ""), ("h2", None, "Next")]
    assert "Intro" in texts(result)  # the heading ends with its line; what follows is a paragraph


def test_a_bullet_between_numbers_starts_their_count_anew_and_a_nested_one_does_not():
    result = laid_out("@node main\n@{number}a\n@{bullet 2}inner\n@{number}b\n@{bullet}c\n@{number}d\n@endnode")

    assert [marker for _, marker, _ in kinds(result)] == ["1.", "◦", "2.", "•", "1."]


def test_a_broken_line_in_a_list_item_stays_in_the_item_without_a_second_marker():
    result = laid_out("@smartwrap\n@node main\n@{bullet}first line@{line}second line\n@endnode")

    assert kinds(result) == [("bullet", "•", "first line"), ("bullet", None, "second line")]


def test_inline_code_and_struck_text_are_attributes_like_bold():
    result = laid_out("@node main\n@{tt}config.yaml@{utt} @{s}old@{us} @{b}@{tt}both@{plain}\n@endnode")

    assert [(span["text"], span["style"]) for span in result["lines"][0]["spans"]] == [
        ("config.yaml", ["tt"]), (" ", []), ("old", ["s"]), (" ", []), ("both", ["b", "tt"])]


def test_a_code_block_keeps_its_lines_and_its_links():
    result = laid_out("@smartwrap\n@node main\n@{code yaml}\nkey: 1\n\n  nested: @{\" x \" link other}\n@{body}\n"
                      "after\n@endnode")

    block = result["lines"][0]
    assert (block["kind"], block["language"]) == ("code", "yaml")
    assert [texts({"lines": [{"spans": row}]})[0] for row in block["rows"]] == ["key: 1", "", "  nested:  x "]
    assert block["rows"][2][1]["link"] == {"guide": "g", "node": "other", "line": 0}
    assert texts(result)[1:] == ["after"]


def test_a_table_has_a_header_row_cells_with_buttons_and_escaped_pipes():
    result = laid_out('@node main\n@{table}\n| Name | What |\n|---|---|\n| @{" x " link other} | a \\| b |\n'
                      "one | two\n@{body}\n@endnode")

    table = result["lines"][0]
    cell_texts = [[texts({"lines": [{"spans": cell}]})[0] for cell in row] for row in table["rows"]]
    assert table["kind"] == "table" and cell_texts == [["Name", "What"], [" x ", "a | b"], ["one", "two"]]
    assert table["rows"][1][0][0]["link"]["node"] == "other"


def test_a_table_loses_nothing_it_is_given():
    result = laid_out("@node main\n@{table}A | B\n- | -\nx@{line}y | z\n3 | 4 @{body}\nafter\n@endnode")

    table = result["lines"][0]
    assert [[texts({"lines": [{"spans": cell}]})[0] for cell in row] for row in table["rows"]] == [
        ["A", "B"], ["-", "-"], ["xy", "z"], ["3", "4"]]
    assert result["problems"] == ["line 4: @{line} does not belong in a table cell"]
    assert texts(result)[1:] == ["after"]


def test_only_the_line_right_under_the_header_is_a_separator():
    result = laid_out("@node main\n@{table}\nKey | Value\n|---|---|\n:---:|---\nset | 1\n--- | ---\n@{body}\n@endnode")

    rows = result["lines"][0]["rows"]
    assert [[texts({"lines": [{"spans": cell}]})[0] for cell in row] for row in rows] == [
        ["Key", "Value"], [":---:", "---"], ["set", "1"], ["---", "---"]]  # a second one right after is a row


def test_a_written_out_body_in_a_cell_does_not_end_the_table():
    result = laid_out("@node main\n@{table}\nWrite | For\n\\@{code} ... \\@{body} | lines as written\n@{body}\n@endnode")

    rows = result["lines"][0]["rows"]
    assert [texts({"lines": [{"spans": cell}]})[0] for cell in rows[1]] == ["@{code} ... @{body}", "lines as written"]
    assert result["problems"] == []


def cell_texts(table):
    return [[texts({"lines": [{"spans": cell}]})[0] for cell in row] for row in table["rows"]]


def test_a_one_line_table_closes_and_a_blank_line_in_a_table_is_no_row():
    one = laid_out("@node main\n@{table}A | B @{body}after\n@endnode")
    assert cell_texts(one["lines"][0]) == [["A", "B"]] and texts(one)[1:] == ["after"] and one["problems"] == []

    blank = laid_out("@node main\n@{table}\nA | B\n\n1 | 2\n@{body}\n@endnode")
    assert cell_texts(blank["lines"][0]) == [["A", "B"], ["1", "2"]]


def test_escapes_decide_where_a_cell_or_a_table_ends():
    result = laid_out("@node main\n@{table}\nPath | Next\nC:\\\\| next\nD:\\\\@{body}\nafter\n@endnode")

    assert cell_texts(result["lines"][0]) == [["Path", "Next"], ["C:\\", "next"], ["D:\\"]]
    assert texts(result)[1:] == ["after"]  # "\\@{body}" is a backslash and a real @{body}


def test_a_region_left_open_by_another_is_named_and_loses_nothing():
    result = laid_out("@node main\n@{code bash}\nls\n@{table}A | B\nc | d\n@{body}\n@endnode")

    code, table = result["lines"]
    assert code["kind"] == "code" and cell_texts(table) == [["A", "B"], ["c", "d"]]
    assert result["problems"] == ["line 2: @{code} without @{body}"]


def test_a_markdown_file_inside_a_code_block_is_shown_not_swallowed(plugins):
    root, plugin = plugins
    folder = plugin("guided", guide='@smartwrap\n@node main "G"\n@{code yaml}\nk: 1\n@embed note.md\nOne\ntwo\n@endnode')
    (folder / "note.md").write_text("# Note", encoding="utf-8")

    page = Library([root]).page("guided", "main")

    assert "<h1>Note</h1>" in markup(page)
    assert page["problems"] == ["line 3: @{code} without @{body}"]
    assert page["lines"][-1]["wrap"] and texts(page)[-1] == "One two"  # the block is over: text wraps again


def test_numbers_count_on_across_code_blocks_and_tables():
    result = laid_out("@node main\n@{number}one\n@{code bash}\nls\n@{body}\n@{number}two\n@{table}\nA\n@{body}\n"
                      "@{number}three\n@{line}\n@{number}four\n@endnode")

    assert [marker for _, marker, _ in kinds(result) if marker] == ["1.", "2.", "3.", "4."]


def test_a_block_command_in_a_code_block_is_named_and_changes_nothing():
    result = laid_out("@node main\n@{number}one\n@{code text}\n@{h1}x\n@{bullet}y\n@{rule}\n@{body}\n"
                      "@{number}two\n@endnode")

    assert [marker for _, marker, _ in kinds(result) if marker] == ["1.", "2."]
    code = next(line for line in result["lines"] if line.get("kind") == "code")
    assert [texts({"lines": [{"spans": row}]})[0] for row in code["rows"]] == ["x", "y"]
    assert result["problems"] == [f"line {n}: @{{{name}}} does not belong in a code block"
                                  for n, name in ((4, "h1"), (5, "bullet"), (6, "rule"))]


def test_alignment_in_a_table_cell_is_named_and_leaves_the_text_after_the_table_alone():
    result = laid_out("@node main\n@{table}\n@{jcenter}A | B\n@{body}\nafter\n@endnode")

    assert result["problems"] == ["line 3: @{jcenter} does not belong in a table cell"]
    assert result["lines"][-1]["align"] == "left"


def test_a_pipe_escape_means_something_only_in_a_table():
    assert texts(laid_out("@node main\na \\| b\n@endnode")) == ["a \\| b"]


@pytest.mark.parametrize("source", ['@{bullet ²}x', '@{" x " link main ²}', "@{number " + "9" * 5000 + "}x"])
def test_odd_digits_are_no_number_and_no_crash(source):
    result = laid_out(f"@node main\n{source}\n@endnode")

    assert result["lines"]


@pytest.mark.parametrize("source", ['@{"' * 8000, "@{table}\nA\n---" + " " * 100_000 + "x\n@{body}"],
                         ids=["unclosed braces", "a table row of blanks"])
def test_a_long_line_costs_no_more_than_its_length(source):
    import time

    start = time.perf_counter()
    laid_out(f"@node main\n{source}\n@endnode")

    assert time.perf_counter() - start < 1.0  # quadratic, each took seconds


def test_an_unclosed_brace_is_named_and_the_commands_after_it_still_work():
    result = laid_out('@node main\n@{b}type @{" to start a label, then @{ub} normal\n@endnode')

    assert result["lines"][0]["spans"][-1] == {"text": " normal", "style": []}
    assert result["problems"] == ["line 2: @{ without its } is shown as text; a literal one is \\@{"]

    quoted = laid_out('@node main\nsay " then @{b}bold@{ub}\n@endnode')  # a quote in the text before a command
    assert {"text": "bold", "style": ["b"]} in quoted["lines"][0]["spans"] and quoted["problems"] == []


def test_command_text_in_a_title_or_a_description_stays_text(plugins):
    guide = parse('@node main "About @{table} and | pipes"\n@endnode', "g")
    index = layout(guide.nodes["index"], guide, lambda target: ("g", target.lower()))
    assert [link["node"] for link in links(index)] == ["main"] and index["problems"] == []

    root, plugin = plugins
    odd = plugin("odd")
    (odd / "plugin.toml").write_text('[plugin]\nname = "odd"\ndescription = "@{\\" q | r \\" link x} \\\\ @"\n',
                                     encoding="utf-8")
    table = next(line for line in Library([root]).page("plugins", "main")["lines"] if line.get("kind") == "table")
    row = next(row for row in cell_texts(table) if row[0].strip() == "odd")
    assert row == ["odd", "", '@{" q | r " link x} \\ @']


def test_a_block_left_open_is_closed_at_the_end_of_the_node_and_named():
    result = laid_out("@node main\n@{code bash}\necho hi\n@endnode")

    assert result["lines"][0]["kind"] == "code"
    assert result["problems"] == ["line 2: @{code} without @{body}"]


def test_a_readme_reflows_its_hard_wrapped_paragraphs_and_the_chat_keeps_its_breaks(plugins):
    from agent_system.utils.markdown_render import markdown_to_html

    root, plugin = plugins
    plugin("wrapped", readme="first half\nsecond half\n")

    assert "<p>first half\nsecond half</p>" in markup(Library([root]).page("wrapped", "main"))
    assert "<br" in markdown_to_html("first half\nsecond half")  # the chat's default is untouched


def test_a_button_may_name_a_documentation_file_next_to_the_guide(plugins):
    root, plugin = plugins
    folder = plugin("guided", guide='@node main "G"\n@{" Design " link docs/design.md} @{" Env " link secrets.env}\n'
                                    '@endnode')
    (folder / "docs").mkdir()
    (folder / "docs" / "design.md").write_text("# Design", encoding="utf-8")
    (folder / "secrets.env").write_text("KEY=1", encoding="utf-8")

    page = Library([root]).page("guided", "main")

    buttons = [span for span in page["lines"][0]["spans"] if len(span) > 2]
    assert buttons[0]["file"] == {"guide": "guided", "file": "docs/design.md"}
    assert buttons[1]["broken"] == "secrets.env"  # only documentation opens as a page


def test_only_a_linked_readme_or_docs_file_opens_as_a_page(plugins):
    root, plugin = plugins
    buttons = " ".join(f'@{{" x " link {relative}}}' for relative in ("README.md", "docs/a.md", "notes.md",
                                                                        "agents/prompts/p.md"))
    # what the README links is what its rendered page shows as a link: HTML too, a path in a code block not
    readme = '# Readme\n\n```\n[x](docs/runbook.md)\n```\n\n<A HREF="docs/c.md">c</A>\n'
    folder = plugin("guided", guide=f'@node main "G"\n{buttons}\n@endnode', readme=readme)
    for relative in ("docs/a.md", "docs/c.md", "docs/sub/b.txt", "docs/runbook.md", "notes.md", "agents/prompts/p.md"):
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text("# Page", encoding="utf-8")
    (folder / "docs" / "a.md").write_text("# A\n\nSee [b](sub/b.txt#top).", encoding="utf-8")
    library = Library([root])

    for relative in ("README.md", "docs/a.md", "./docs/sub/b.txt", "docs/c.md"):  # b through a: a page links on
        assert library.page("guided", file=relative)["file"] == relative
    # working notes and an agent's prompt are not for every reader; nor is a file in docs/ nobody links
    for relative in ("notes.md", "agents/prompts/p.md", "docs/runbook.md"):
        with pytest.raises(KeyError):
            library.page("guided", file=relative)


def test_a_plugin_folder_with_capitals_is_reachable(plugins):
    root, plugin = plugins
    plugin("MyPlugin", readme="# Mine")
    library = Library([root])

    assert library.page("MyPlugin", "main")["guide"] == "myplugin"
    assert library.resolve("scarabhive", "MyPlugin/main") == ("myplugin", "main")


def test_a_readme_page_follows_its_manifest_and_a_late_embed_appears(plugins):
    root, plugin = plugins
    folder = plugin("readme_only", readme="# R")
    assert "Probe readme_only" in plain_text(Library([root]).page("readme_only", "main"))
    (folder / "plugin.toml").write_text('[plugin]\nname = "readme_only"\ndescription = "New words here"\n',
                                        encoding="utf-8")
    assert "New words here" in plain_text(Library([root]).page("readme_only", "main"))

    guided = plugin("guided", guide='@node main "G"\n@embed later.yaml\n@endnode')
    assert Library([root]).guides["guided"].warnings  # not there yet
    (guided / "later.yaml").write_text("k: 1", encoding="utf-8")
    assert "k: 1" in plain_text(Library([root]).page("guided", "main"))


def test_a_byte_order_mark_is_no_text(plugins):
    root, plugin = plugins
    folder = plugin("bommed", guide='﻿@database "Bom title"\n@node main "M"\n@{" a " link docs/a.md}\n@endnode')
    (folder / "docs").mkdir()
    (folder / "docs" / "a.md").write_text("﻿# Title", encoding="utf-8")
    library = Library([root])

    assert library.guides["bommed"].title == "Bom title" and library.guides["bommed"].warnings == []
    assert "<h1>Title</h1>" in markup(library.page("bommed", file="docs/a.md"))


def test_a_number_starting_a_line_in_a_readme_sentence_is_no_list(plugins):
    root, plugin = plugins
    plugin("sentence", readme="A priority outside 1 to\n10. A refusal answers 400.\n\nSteps:\n1. one\n2. two\n")

    html = markup(Library([root]).page("sentence", "main"))

    assert "outside 1 to\n10. A refusal answers 400.</p>" in html  # the 10 stays
    assert "<li>one</li>" in html and "<li>two</li>" in html  # a list that starts at 1 still interrupts


def test_a_readme_list_keeps_its_dashes_and_shows_right_under_a_paragraph(plugins):
    root, plugin = plugins
    plugin("listed", readme="Needs:\n- `ib_async` - the API client\n- pandas\n\nOpen Mo - Fr - Sa, closed on holidays.\n")

    html = markup(Library([root]).page("listed", "main"))

    assert "<li><code>ib_async</code> - the API client</li>" in html and "<li>pandas</li>" in html
    assert "<p>Open Mo - Fr - Sa, closed on holidays.</p>" in html  # the chat's list rescue stays out


def test_a_document_keeps_its_fences_and_an_indented_item_continues_the_paragraph():
    from agent_system.utils.markdown_render import markdown_to_html

    def document(source):
        return markdown_to_html(source, line_breaks=False)

    assert "<pre>" not in document("Needs:\n\t- pandas\n")  # a tab is four columns: the line continues, as on GitHub
    assert "~~~\nText\n- item\n" in document("```\n~~~\nText\n- item\n```\n")  # a tilde line in backticks is code
    assert "<li>a</li>" in document("````\n```\n````\nIntro\n- a\n")  # a shorter fence does not close a longer one


def test_lines_end_at_line_breaks_only_and_a_stray_byte_costs_only_itself():
    from agent_system.ui.amigaguide import decode

    result = laid_out("@node main\nfirst\x0csame line\nthird\n@endnode")
    assert [line["n"] for line in result["lines"]] == [2, 3]

    assert decode("Wait… then".encode("cp1252")) == "Wait… then"  # Windows text, no UTF-8 in it
    assert decode("ä and ".encode() + b"\x96") == "ä and \N{REPLACEMENT CHARACTER}"
    german = "Das macht »Spaß«. Größe und Maße für Übung."  # "ß«" in cp1252 is a valid UTF-8 pair
    assert decode(german.encode("cp1252")) == german


def test_markdown_is_not_a_guide_format():
    guide = parse("@node main\n@markdown\n# Title\n@endnode", "g")

    assert guide.warnings == ["line 2: unknown command @markdown"]


def test_a_readme_opens_its_docs_in_the_viewer_and_keeps_the_rest_as_text(plugins):
    root, plugin = plugins
    folder = plugin("docs", readme="See [design](docs/design.md), [schema](schema.yaml), [up](../other/x.md), "
                                   "[web](https://e.org/).\n\n![local](docs/pic.png) ![remote](https://x.org/b.png)\n")
    (folder / "docs").mkdir()
    (folder / "docs" / "design.md").write_text("# Design\n\n[next](sibling.md) [secret](../secrets.env)\n",
                                               encoding="utf-8")
    (folder / "docs" / "sibling.md").write_text("sibling", encoding="utf-8")
    (folder / "docs" / "pic.png").write_bytes(PNG)
    (folder / "schema.yaml").write_text("x: 1", encoding="utf-8")
    (folder / "secrets.env").write_text("KEY=1", encoding="utf-8")
    library = Library([root])

    readme = library.page("docs", "main")
    html = markup(readme)
    assert 'href="?guide=docs&amp;file=docs%2Fdesign.md" data-guide="docs" data-file="docs/design.md">design</a>' in html
    assert "<a>schema</a>" in html and "<a>up</a>" in html and 'target="_blank"' in html
    assert 'src="/api/help/asset?guide=docs&amp;path=docs%2Fpic.png"' in html
    assert "remote" in html and "x.org" not in html  # a remote image is not loaded; its alt text stands in
    assert readme["problems"] == []  # a README's links to code are not the guide's mistakes

    design = library.page("docs", file="docs/design.md")
    assert (design["node"], design["file"], design["title"]) == (None, "docs/design.md", "design.md")
    assert "<h1>Design</h1>" in markup(design) and 'data-file="docs/sibling.md"' in markup(design)
    assert "<a>secret</a>" in markup(design)  # only documentation files open as pages
    assert design["nav"]["contents"] == {"guide": "docs", "node": "main"}
    for file in ("secrets.env", "schema.yaml", "../other/x.md", "docs/missing.md"):
        with pytest.raises(KeyError):
            library.page("docs", file=file)


@pytest.mark.parametrize("path", ["//evil.example/share/x.png", "\\\\evil.example\\share\\x.png", "C:/x.png",
                                  "C:x.png", "/etc/passwd"])
def test_an_absolute_or_network_path_is_refused_before_the_disk_is_asked(tmp_path, monkeypatch, path):
    from agent_system.ui import amigaguide

    def asked(self, *args, **kwargs):
        raise AssertionError(f"resolve() was called for {self}")

    monkeypatch.setattr(amigaguide.Path, "resolve", asked)

    assert amigaguide.inside(tmp_path, path) is None


def test_a_file_too_large_to_render_is_named_not_shown(plugins, monkeypatch):
    from agent_system.ui import amigaguide

    root, plugin = plugins
    folder = plugin("big", readme="See [the log](docs/log.md).")
    (folder / "docs").mkdir()
    (folder / "docs" / "log.md").write_text("# Log\n\n" + "entry\n" * 50, encoding="utf-8")
    monkeypatch.setattr(amigaguide, "MAX_FILE_BYTES", 100)

    page = Library([root]).page("big", file="docs/log.md")

    assert markup(page) == "" and "log.md is too large to show here" in plain_text(page)


def test_a_file_that_cannot_be_read_costs_its_own_guide_only(plugins, monkeypatch):
    root, plugin = plugins
    plugin("locked", readme="# Locked")
    plugin("fine", readme="# Fine")
    (plugin("guided", guide='@node main "G"\n@embed held.txt\n@endnode') / "held.txt").write_text("x", encoding="utf-8")
    real = Path.read_bytes

    def read_bytes(self):
        if self.parent.name == "locked" or self.name == "held.txt":
            raise PermissionError(f"{self} is held by another process")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    library = Library([root])

    assert "<h1>Fine</h1>" in markup(library.page("fine", "main")) and "locked" not in library.guides
    assert library.guides["guided"].warnings == [
        "line 2: @embed needs a node and a readable file next to the guide, got ['held.txt']"]


def test_a_panel_knows_the_guide_of_its_plugin_by_the_instance_type(plugins):
    from agent_system.config.models import ToolServerConfig
    from agent_system.ui.help import panel_guides

    root, plugin = plugins
    plugin("guided", guide='@node main "G"\nx\n@endnode')
    plugin("readme_only", readme="# R")
    plugin("bare")
    plugin("scarabhive", readme="# Clash")  # named like the manual: its docs are left out, so no guide of its own
    plugin("plugins", readme="# Clash")  # named like the generated plugin list: the same
    config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[root], servers={
        "workspace_guided": ToolServerConfig(type="guided"),
        "readme_only": ToolServerConfig(type="bare"),  # named like a documented plugin, runs as another one
        "my_sam": ToolServerConfig(type="coder_sam"), "coder_sam": ToolServerConfig(type="guided"),  # inherits
        "loop_a": ToolServerConfig(type="loop_b"), "loop_b": ToolServerConfig(type="loop_a"),  # never loads
        "guided": ToolServerConfig(type="guided", tools={"allowed": ["a/*"]}),
        # a list the inheritance cannot merge: the loader runs the entry as written, a guided one
        "mixed": ToolServerConfig(type="guided", tools={"allowed": ["+x/*", "y/*"]}),
    }))

    guides = panel_guides(config, ["workspace_guided", "readme_only", "bare", "scarabhive", "plugins", "unknown",
                                   "guided", "my_sam", "loop_a", "mixed"])

    # an instance is its entry's type, followed through other entries; one without an entry is its own name
    assert guides == {"workspace_guided": "guided", "guided": "guided", "my_sam": "guided", "mixed": "guided"}
    assert panel_guides(SimpleNamespace(plugins=None), ["guided"]) == {}


def test_a_plugin_folder_that_cannot_be_read_costs_its_own_plugins_only(plugins, tmp_path, monkeypatch):
    from agent_system.ui import help as help_module

    root, plugin = plugins
    plugin("locked", readme="# Locked")
    plugin("fine", readme="# Fine")
    closed = tmp_path / "closed"
    closed.mkdir()
    real_iterdir, real_metadata = Path.iterdir, help_module.load_plugin_metadata

    def iterdir(self):
        if self == closed:
            raise PermissionError(f"{self}: access denied")
        return real_iterdir(self)

    def metadata(folder):
        if folder.name == "locked":
            raise PermissionError(f"{folder}: access denied")
        return real_metadata(folder)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(help_module, "load_plugin_metadata", metadata)

    # the catalogue asks for every plugin's docs: one it cannot read must not take the others with it
    assert [doc.id for doc in help_module.plugin_docs([closed, root])] == ["fine"]


def test_search_lays_every_node_out_once(plugins, monkeypatch):
    from agent_system.ui import help as help_module

    root, plugin = plugins
    plugin("guided", guide='@node main "Main"\nzebra\n@endnode')
    library = Library([root])
    calls = []
    real = help_module.layout
    monkeypatch.setattr(help_module, "layout", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))

    library.search("zebra")
    first = len(calls)
    library.search("zebra")

    assert first > 0 and len(calls) == first


def test_every_guide_in_the_repository_reads_cleanly():
    """The authors' check: no structural warning, no dead link, no unknown attribute -- manual and plugins."""
    library = Library(PLUGIN_DIRS)
    mistakes = {}
    for guide_id, guide in library.guides.items():
        if guide.warnings:
            mistakes[guide_id] = guide.warnings
        for key in guide.nodes:
            problems = library.page(guide_id, key)["problems"]
            if problems:
                mistakes[f"{guide_id}/{key}"] = problems

    assert mistakes == {}
    assert {"main", "help", "authoring"} <= set(library.guides["scarabhive"].nodes)


# --- the routes ------------------------------------------------------------

@pytest.fixture
def client(plugins):
    root, plugin = plugins
    folder = plugin("guided", guide='@node main "Guided"\nquokka @{" notes " link docs/notes.md}\n@endnode')
    (folder / "pic.png").write_bytes(PNG)
    (folder / "docs").mkdir()
    (folder / "docs" / "pic.png").write_bytes(PNG)
    (folder / "tests").mkdir()
    (folder / "tests" / "pic.png").write_bytes(PNG)
    (folder / "docs" / "notes.md").write_text("# Notes", encoding="utf-8")
    (folder / "notes.md").write_text("# Working notes", encoding="utf-8")
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[str(root)]))
    return TestClient(app)


def test_the_routes_serve_a_node_a_file_a_search_and_the_panel(client, monkeypatch, tmp_path):
    from tests.ui.test_help_index import use_fake_model
    use_fake_model(monkeypatch, tmp_path / "index.json")  # not the real model, not the real data directory
    assert client.get("/api/help/node").json()["guide"] == "scarabhive"
    assert client.get("/api/help/node", params={"guide": "guided", "node": "nope"}).status_code == 404
    assert client.get("/api/help/node", params={"guide": "nope"}).status_code == 404
    notes = client.get("/api/help/node", params={"guide": "guided", "file": "docs/notes.md"}).json()
    assert notes["file"] == "docs/notes.md" and "<h1>Notes</h1>" in markup(notes)
    assert client.get("/api/help/node", params={"guide": "guided", "file": "notes.md"}).status_code == 404
    assert client.get("/api/help/asset", params={"guide": "guided", "path": "a" + chr(0) + "b.png"}).status_code == 404
    assert client.get("/api/help/node", params={"guide": "guided", "file": "a" + chr(0) + "b.md"}).status_code == 404
    assert client.get("/api/help/node", params={"guide": "guided", "file": "plugin.toml"}).status_code == 404
    found = client.get("/api/help/search", params={"q": "quokka"}).json()
    assert (found["hits"][0]["guide"], found["hits"][0]["node"]) == ("guided", "main")
    page = client.get("/ui/panels/help")
    assert page.status_code == 200 and "/static/kit/guide.js" in page.text


def test_an_image_is_served_from_the_guide_folder_only_and_runs_nothing(client):
    image = client.get("/api/help/asset", params={"guide": "guided", "path": "pic.png"})

    assert image.status_code == 200 and image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert "sandbox" in image.headers["content-security-policy"]
    assert client.get("/api/help/asset", params={"guide": "guided", "path": "docs/pic.png"}).status_code == 200
    for path in ("../guided/pic.png/..", "plugin.toml", "../../x.png", "missing.png", "//evil.example/s/x.png",
                 "tests/pic.png"):  # the top of the folder and docs/, as for pages: not a test's fixture
        assert client.get("/api/help/asset", params={"guide": "guided", "path": path}).status_code == 404, path


def test_the_routes_answer_without_a_plugins_section():
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(plugins=None)

    assert TestClient(app).get("/api/help/node").json()["guide"] == "scarabhive"
