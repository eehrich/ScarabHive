// File upload module for handling multimodal attachments (images, audio, text)
(function (global) {
  const fileUploadModule = {};

  let selectedFiles = [];
  const MAX_FILE_SIZE = 20 * 1024 * 1024; // 20MB default
  
  // Supported file formats by type
  const SUPPORTED_FORMATS = {
    image: ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp'],
    audio: ['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav', 'audio/ogg', 'audio/flac', 'audio/webm', 'audio/m4a', 'audio/x-m4a'],
    text: ['text/plain', 'text/markdown', 'text/csv', 'application/json', 'application/xml', 'text/html', 'text/x-python', 'text/javascript', 'text/css']
  };
  
  // File extensions for fallback detection
  const EXTENSION_MAP = {
    // Images
    '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.gif': 'image', '.webp': 'image',
    // Audio
    '.mp3': 'audio', '.wav': 'audio', '.ogg': 'audio', '.flac': 'audio', '.m4a': 'audio', '.webm': 'audio',
    // Text
    '.txt': 'text', '.md': 'text', '.csv': 'text', '.json': 'text', '.xml': 'text', 
    '.html': 'text', '.htm': 'text', '.py': 'text', '.js': 'text', '.ts': 'text',
    '.css': 'text', '.yaml': 'text', '.yml': 'text', '.log': 'text', '.ini': 'text',
    '.cfg': 'text', '.conf': 'text', '.sh': 'text', '.bat': 'text', '.ps1': 'text',
    '.sql': 'text', '.r': 'text', '.java': 'text', '.c': 'text', '.cpp': 'text',
    '.h': 'text', '.hpp': 'text', '.rs': 'text', '.go': 'text', '.rb': 'text'
  };

  function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
  }
  
  function getFileType(file) {
    // Try MIME type first
    if (file.type) {
      for (const [type, mimes] of Object.entries(SUPPORTED_FORMATS)) {
        if (mimes.includes(file.type)) {
          return type;
        }
      }
    }
    
    // Fallback to extension
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    return EXTENSION_MAP[ext] || null;
  }
  
  function getFileIcon(fileType) {
    switch (fileType) {
      case 'image':
        return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
          <circle cx="8.5" cy="8.5" r="1.5"/>
          <polyline points="21 15 16 10 5 21"/>
        </svg>`;
      case 'audio':
        return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 18V5l12-2v13"/>
          <circle cx="6" cy="18" r="3"/>
          <circle cx="18" cy="16" r="3"/>
        </svg>`;
      case 'text':
        return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
        </svg>`;
      default:
        return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
          <polyline points="13 2 13 9 20 9"/>
        </svg>`;
    }
  }

  function isValidFile(file) {
    const fileType = getFileType(file);
    
    if (!fileType) {
      return { valid: false, error: 'Unsupported file type', fileType: null };
    }
    
    if (file.size > MAX_FILE_SIZE) {
      return { valid: false, error: 'File too large (max 20MB)', fileType };
    }
    
    return { valid: true, fileType };
  }

  function createFilePreviewItem(file, index) {
    const item = document.createElement('div');
    item.className = 'attached-file-item';
    item.dataset.index = index;

    const validation = isValidFile(file);

    // File icon based on type
    const icon = document.createElement('div');
    icon.className = 'attached-file-icon';
    icon.innerHTML = getFileIcon(validation.fileType);

    // File name
    const name = document.createElement('div');
    name.className = 'attached-file-name';
    name.textContent = file.name;
    name.title = file.name;

    // File size and type badge
    const meta = document.createElement('div');
    meta.className = 'attached-file-size';
    const typeBadge = validation.fileType ? ` [${validation.fileType}]` : '';
    meta.textContent = formatFileSize(file.size) + typeBadge;

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.className = 'attached-file-remove';
    removeBtn.innerHTML = '×';
    removeBtn.type = 'button';
    removeBtn.title = 'Remove file';
    removeBtn.onclick = () => removeFile(index);

    item.appendChild(icon);
    item.appendChild(name);
    item.appendChild(meta);
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
    return selectedFiles.filter(file => isValidFile(file).valid);
  }
  
  function getFilesByType() {
    const result = { images: [], audio: [], text: [] };
    for (const file of selectedFiles) {
      const validation = isValidFile(file);
      if (validation.valid && validation.fileType) {
        if (validation.fileType === 'image') result.images.push(file);
        else if (validation.fileType === 'audio') result.audio.push(file);
        else if (validation.fileType === 'text') result.text.push(file);
      }
    }
    return result;
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
    
    // Update file input to accept more types
    fileInput.accept = 'image/*,audio/*,.txt,.md,.csv,.json,.xml,.html,.py,.js,.ts,.css,.yaml,.yml,.log';

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

    // Paste support (images and text)
    document.addEventListener('paste', (e) => {
      const items = e.clipboardData.items;
      const files = [];
      
      for (let i = 0; i < items.length; i++) {
        // Check for image or file types
        if (items[i].kind === 'file') {
          const file = items[i].getAsFile();
          if (file && isValidFile(file).valid) {
            files.push(file);
          }
        }
      }
      
      if (files.length > 0) {
        addFiles(files);
        e.preventDefault();
      }
    });
    
    console.log('File upload module initialized (supports images, audio, text)');
  }

  // Export API
  fileUploadModule.init = init;
  fileUploadModule.getFiles = getFiles;
  fileUploadModule.getFilesByType = getFilesByType;
  fileUploadModule.hasValidFiles = hasValidFiles;
  fileUploadModule.clearFiles = clearFiles;
  fileUploadModule.addFiles = addFiles;
  fileUploadModule.isValidFile = isValidFile;
  fileUploadModule.getFileType = getFileType;

  global.fileUploadModule = fileUploadModule;
})(window);
