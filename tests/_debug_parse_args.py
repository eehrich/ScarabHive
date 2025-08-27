import sys
from pathlib import Path
import argparse

# argv to inspect (same as failing test)
argv = ["agent-cli", "plugins", "info", "raw_example", "--raw", "--format", "json"]
print('input argv:', argv)
# prelim parse
prelim = argparse.ArgumentParser(add_help=False)
prelim.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")))
prelim.add_argument("-v", "--verbose", dest="verbose", action="store_true")
prelim.add_argument("--color", dest="color", choices=["auto","always","never"], default="auto")
prelim.add_argument("--no-color", dest="no_color", action="store_true")
prelim.add_argument("--no-stream", dest="no_stream", action="store_true")
prelim.add_argument("--raw", dest="raw", action="store_true", help="Output raw JSON result instead of pretty printing")
ns, rest = prelim.parse_known_args(argv[1:])
print('prelim ns.raw=', ns.raw)
print('prelim rest=', rest)
# reconstruct final argv same as cli
final_args = []
if getattr(ns, "config", None):
    final_args.extend(["--config", ns.config])
if getattr(ns, "verbose", False):
    final_args.append("--verbose")
if getattr(ns, "no_color", False):
    final_args.append("--no-color")
elif getattr(ns, "color", "auto") != "auto":
    final_args.extend(["--color", ns.color])
if getattr(ns, "no_stream", False):
    final_args.append("--no-stream")
if getattr(ns, "raw", False):
    final_args.append("--raw")
# insert implicit run if needed
known = ("plugins", "run", "-h", "--help")
rest2 = list(rest)
if rest2:
    if not rest2[0].startswith("-") and rest2[0] not in known:
        rest2.insert(0, "run")
else:
    rest2 = []
argv2 = [argv[0]] + final_args + rest2
print('final argv2:', argv2)
# final parse
parser = argparse.ArgumentParser(description='Agent System CLI')
parser.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")), help="Path to config")
parser.add_argument("-v","--verbose", action="store_true", help="Print progress messages")
parser.add_argument("--color", dest="color", choices=["auto","always","never"], default="auto", help="Colorize output (auto|always|never)")
parser.add_argument("--no-color", dest="no_color", action="store_true")
parser.add_argument("--no-stream", dest="no_stream", action="store_true")
parser.add_argument("--raw", dest="raw", action="store_true")
subparsers = parser.add_subparsers(dest='subcommand')
runp = subparsers.add_parser('run')
runp.add_argument('task', nargs='?', default='What can you do?')
plugp = subparsers.add_parser('plugins')
plugp.add_argument('action', choices=['list','info','enable','disable','search','status'], nargs='?', default='list')
plugp.add_argument('name', nargs='?')
plugp.add_argument('--format', dest='out_format', choices=['json','table'], default='table')
args = parser.parse_args(argv2[1:])
print('final args:', args)
print('args.raw=', getattr(args,'raw', None))
