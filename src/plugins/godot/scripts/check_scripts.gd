# Parse check for a whole project, run headless by the godot plugin:
#
#   godot --headless --path <project> -s check_scripts.gd
#
# Loading a script compiles it; one that does not parse makes the loader
# print a SCRIPT ERROR block to stderr. Measured on 4.7: load() still
# returns a GDScript object for it (not null), so the failure is read off
# can_instantiate(). Godot has no "check everything" flag (--check-only takes
# exactly one script), hence the walk. addons/ and .godot/ are skipped:
# third-party code and the import cache are not what the agent is editing.
extends SceneTree


func _init() -> void:
	var scripts := _collect("res://")
	var failed := 0
	for path in scripts:
		var script = load(path)
		if script == null or not (script is Script) or not script.can_instantiate():
			failed += 1
			# Named per script: a failure that printed no SCRIPT ERROR block
			# (an abstract class, a dependency that did not load) is otherwise
			# a count without a file.
			print("FAILED %s" % path)
	print("CHECKED %d FAILED %d" % [scripts.size(), failed])
	quit(1 if failed > 0 else 0)


func _collect(dir_path: String) -> PackedStringArray:
	var found := PackedStringArray()
	var dir := DirAccess.open(dir_path)
	if dir == null:
		return found
	dir.list_dir_begin()
	var name := dir.get_next()
	while name != "":
		if name != "." and name != "..":
			var full := dir_path.path_join(name)
			if dir.current_is_dir():
				if name != ".godot" and name != "addons" and not name.begins_with("."):
					found.append_array(_collect(full))
			elif name.ends_with(".gd"):
				found.append(full)
		name = dir.get_next()
	dir.list_dir_end()
	return found
