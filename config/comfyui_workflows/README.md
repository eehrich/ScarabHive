# ComfyUI Workflows Directory
# Place your ComfyUI workflow JSON files here.

## How to export workflows from ComfyUI:
1. Build your workflow in ComfyUI
2. Click "Save (API Format)" to export the workflow as JSON
3. Save the file to this directory
4. Add configuration to config/plugins.yaml under comfyui.workflows

## Example workflow configuration in plugins.yaml:
```yaml
comfyui:
  workflows:
    - id: my_workflow
      name: "My Custom Workflow"
      description: "Description of what this workflow does"
      category: image_generation
      workflow_file: my_workflow.json
      parameters:
        - name: prompt
          type: string
          required: true
          node_id: "3"
          field: inputs.text
```

## Tips:
- Use descriptive workflow IDs (e.g., sd15_txt2img, flux_img2img)
- Group similar workflows by category
- Document required parameters for each workflow
- Test workflows manually in ComfyUI before automating
