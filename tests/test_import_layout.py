"""Schutz gegen den pythonpath-Footgun von `pythonpath = src tests`.

pytest.ini legt tests/ als zweiten Import-Root auf sys.path (fuer geteilte
Test-Helper wie tool_execution_test_helpers.py). Dadurch wird jedes
tests/-Unterverzeichnis als Top-Level-NAMESPACE-Package importierbar —
tests/mcp, tests/config, tests/utils etc. kollidieren namentlich mit echten
Import-Roots (site-packages `mcp`!, src-Packages). Das haelt heute nur,
weil REGULAERE Packages (mit __init__.py) Namespace-Portionen immer schlagen.

Ein einziges kuenftiges __init__.py unter tests/ (z.B. tests/mcp/__init__.py)
wuerde das gleichnamige echte Package still shadowen und Imports fernab der
Ursache brechen. Dieser Meta-Test macht daraus einen lauten, lokalisierten
Fehler.
"""
from pathlib import Path

TESTS_DIR = Path(__file__).parent


def test_no_init_py_in_direct_test_subdirs():
    # Nur DIREKTE Kinder von tests/ sind gefaehrlich: erst ein
    # tests/<name>/__init__.py macht <name> zum regulaeren Package, das ein
    # gleichnamiges echtes Package shadowen kann. Tiefere __init__.py
    # (z.B. Fixture-Packages unter tests/fixtures/real_plugin/) sind ok.
    offenders = [
        d.name for d in TESTS_DIR.iterdir()
        if d.is_dir() and d.name != "__pycache__" and (d / "__init__.py").exists()
    ]
    assert not offenders, (
        f"__init__.py in direkten tests/-Unterverzeichnissen gefunden: "
        f"{offenders} — verboten, weil tests/ auf dem pythonpath liegt und "
        f"ein solches __init__.py das gleichnamige ECHTE Package "
        f"(site-packages `mcp`, src-Packages) still shadowen wuerde. "
        f"Geteilte Helper gehoeren als flache Module direkt nach tests/ "
        f"(siehe tool_execution_test_helpers.py)."
    )
