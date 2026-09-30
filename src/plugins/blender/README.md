# Blender

Lets an agent work in a Blender running on this machine: read the scene and single objects, run Python (bpy)
inside Blender, take a picture of the 3D viewport and export the scene or the selection into the workspace. It
talks directly to the BlenderMCP addon's local socket -- no bridge process, no Python dependencies.

- **Tools** `status`, `scene`, `object`, `execute`, `screenshot`, `export` -- screenshots and exports are
  written by Blender into the output directory (`data/workspace/blender`) and nowhere else; `execute` runs any
  Python with Blender's rights.
- **Agent** `blender_agent` -- models and edits a scene look-change-look and exports the result; admins only.
- No hooks, no panel.

The instance and the agent come with the plugin (`agents/blender_agent.yaml`); another agent gets the tools with
`+blender/*` in its tool list. In Blender, enable the addon and click **Connect to MCP server** in the N-panel's
**BlenderMCP** tab.

The full manual -- getting Blender ready, every parameter and answer, the export formats, the path rules and
what the socket may reach, the agent and the server settings -- is the plugin's guide, `blender.guide`, in the
Help panel.
