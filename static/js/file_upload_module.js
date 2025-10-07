// File upload module for handling image attachments
(function (global) {
  const fileUploadModule = {};

  let selectedFiles = [];
  const MAX_FILE_SIZE = 20 * 1024 * 1024; // 20MB default
  const SUPPORTED_FORMATS = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp'];

  function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
  }

  function isValidImageFile(file) {
    if (!file.type.startsWith('image/')) {
      return { valid: false, error: 'Not an image file' };
    }
    if (!SUPPORTED_FORMATS.includes(file.type)) {
      return { valid: false, error: 'Unsupported format' };
    }
    if (file.size > MAX_FILE_SIZE) {
      return { valid: false, error: 'File too large (max 20MB)' };
    }
    return { valid: true };
  }

  function createFilePreviewItem(file, index) {
    const item = document.createElement('div');
    item.className = 'attached-file-item';
    item.dataset.index = index;

    const validation = isValidImageFile(file);

    // File icon (using SVG)
    const icon = document.createElement('div');
    icon.className = 'attached-file-icon';
    icon.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
        <circle cx="8.5" cy="8.5" r="1.5"/>
        <polyline points="21 15 16 10 5 21"/>
      </svg>
    `;

    // File name
    const name = document.createElement('div');
    name.className = 'attached-file-name';
    name.textContent = file.name;
    name.title = file.name;

    // File size
    const size = document.createElement('div');
    size.className = 'attached-file-size';
    size.textContent = formatFileSize(file.size);

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.className = 'attached-file-remove';
    removeBtn.innerHTML = '×';
    removeBtn.type = 'button';
    removeBtn.title = 'Remove file';
    removeBtn.onclick = () => removeFile(index);

    item.appendChild(icon);
    item.appendChild(name);
    item.appendChild(size);
    item.appendChild(removeBtn);

    if (!validation.valid) {
      item.style.borderColor = '#ef4444';
      item.style.color = '#ef4444';
      name.textContent += ` (${validation.error})`;
    }

    return item;
  }

  function updatePreview() {
    const preview = document.getElementById('attachedFiles');
    if (!preview) return;

    preview.innerHTML = '';
    
    if (selectedFiles.length === 0) {
      preview.style.display = 'none';
      return;
    }
    
    preview.style.display = 'flex';
    selectedFiles.forEach((file, index) => {
      const item = createFilePreviewItem(file, index);
      preview.appendChild(item);
    });
  }

  function addFiles(files) {
    const newFiles = Array.from(files);
    selectedFiles = [...selectedFiles, ...newFiles];
    updatePreview();
  }

  function removeFile(index) {
    selectedFiles.splice(index, 1);
    updatePreview();
  }

  function clearFiles() {
    selectedFiles = [];
    updatePreview();
  }

  function getFiles() {
    return selectedFiles.filter(file => isValidImageFile(file).valid);
  }

  function hasValidFiles() {
    return getFiles().length > 0;
  }

  // Initialize file upload UI
  function init() {
    const fileInput = document.getElementById('fileInput');
    const inputContainer = document.querySelector('.input-container');
    const textarea = document.getElementById('task');
    
    if (!fileInput) {
      console.warn('File upload input not found');
      return;
    }

    // Handle file selection
    fileInput.addEventListener('change', (e) => {
      if (e.target.files.length > 0) {
        addFiles(e.target.files);
        // Reset input so same file can be selected again
        fileInput.value = '';
      }
    });

    // Drag and drop support on textarea
    if (textarea) {
      textarea.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
        textarea.style.borderColor = '#3b82f6';
      });

      textarea.addEventListener('dragleave', (e) => {
        e.preventDefault();
        e.stopPropagation();
        textarea.style.borderColor = '';
      });

      textarea.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        textarea.style.borderColor = '';
        
        const files = e.dataTransfer.files;
        if (files.length > 0) {
          addFiles(files);
        }
      });
    }

    // Paste support
    document.addEventListener('paste', (e) => {
      const items = e.clipboardData.items;
      const files = [];
      
      for (let i = 0; i < items.length; i++) {
        if (items[i].type.indexOf('image') !== -1) {
          const file = items[i].getAsFile();
          if (file) files.push(file);
        }
      }
      
      if (files.length > 0) {
        addFiles(files);
        e.preventDefault();
      }
    });
    
    console.log('File upload module initialized');
  }

  // Export API
  fileUploadModule.init = init;
  fileUploadModule.getFiles = getFiles;
  fileUploadModule.hasValidFiles = hasValidFiles;
  fileUploadModule.clearFiles = clearFiles;
  fileUploadModule.addFiles = addFiles;

  global.fileUploadModule = fileUploadModule;
})(window);
