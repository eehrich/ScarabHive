# Script Interpreter

A Python sandbox for agents: instead of guessing at arithmetic, an agent sends a few lines of code -- sum figures,
work out a percentage, count, sort -- and gets back what they printed. The code runs in the server as a restricted
subset of Python with no imports and no access to files, the network or the server itself, under time and size
limits. Each chat session keeps its own variables from one call to the next. The same sandbox runs the scripts of
the `tool_script` plugin.

- **Tools** `script_interpreter_execute` -- runs the code and answers its output, the session's variables and the
  time, or the error with its line; `script_interpreter_reset` -- clears the session's sandbox.

Enable it in `config/plugins.yaml` (`script_interpreter: {type: script_interpreter, enabled: true}`) and allow
`+script_interpreter/*` in an agent's tool list.

The full manual -- both tools and their answers, what the language supports, every limit and the server settings --
is the plugin's guide, `script_interpreter.guide`, in the Help panel.
