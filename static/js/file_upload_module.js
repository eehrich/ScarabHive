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
    item.className = 'file-preview-item';
    item.dataset.index = index;

    const validation = isValidImageFile(file);
    if (!validation.valid) {
      item.classList.add('file-preview-item-error');
    }

    // Create image preview
    const img = document.createElement('img');
    img.alt = file.name;
    
    // Read file and create preview
    const reader = new FileReader();
    reader.onload = (e) => {
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);

    // File name
    const name = document.createElement('div');
    name.className = 'file-preview-item-name';
    name.textContent = file.name;
    name.title = file.name;

    // File size
    const size = document.createElement('div');
    size.className = 'file-preview-item-size';
    size.textContent = formatFileSize(file.size);

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.className = 'file-preview-item-remove';
    removeBtn.innerHTML = '×';
    removeBtn.type = 'button';
    removeBtn.onclick = () => removeFile(index);

    item.appendChild(removeBtn);
    item.appendChild(img);
    item.appendChild(name);
    item.appendChild(size);

    if (!validation.valid) {
      const errorMsg = document.createElement('div');
      errorMsg.className = 'file-preview-item-error-msg';
      errorMsg.textContent = validation.error;
      item.appendChild(errorMsg);
    }

    return item;
  }

  function updatePreview() {
    const preview = document.getElementById('filePreview');
    if (!preview) return;

    preview.innerHTML = '';
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
    const inputWrapper = document.querySelector('.input-wrapper');
    
    if (!fileInput || !inputWrapper) {
      console.warn('File upload elements not found');
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

    // Drag and drop support
    inputWrapper.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
      inputWrapper.classList.add('drag-over');
    });

    inputWrapper.addEventListener('dragleave', (e) => {
      e.preventDefault();
      e.stopPropagation();
      inputWrapper.classList.remove('drag-over');
    });

    inputWrapper.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      inputWrapper.classList.remove('drag-over');
      
      const files = e.dataTransfer.files;
      if (files.length > 0) {
        addFiles(files);
      }
    });

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
  }

  // Export API
  fileUploadModule.init = init;
  fileUploadModule.getFiles = getFiles;
  fileUploadModule.hasValidFiles = hasValidFiles;
  fileUploadModule.clearFiles = clearFiles;
  fileUploadModule.addFiles = addFiles;

  global.fileUploadModule = fileUploadModule;
})(window);
