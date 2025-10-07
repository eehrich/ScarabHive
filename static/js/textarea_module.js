// Textarea auto-resize and keyboard shortcuts module
(function() {
  function init() {
    const textarea = document.getElementById('task');
    if (!textarea) {
      console.warn('Textarea #task not found');
      return;
    }
    
    // Auto-resize function
    function autoResize() {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
    }
    
    // Listen to input events
    textarea.addEventListener('input', autoResize);
    
    // Keyboard shortcuts
    textarea.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        if (e.ctrlKey || e.metaKey) {
          // Ctrl+Enter or Cmd+Enter: submit form
          e.preventDefault();
          const form = document.getElementById('f');
          if (form) {
            const submitEvent = new Event('submit', { cancelable: true, bubbles: true });
            form.dispatchEvent(submitEvent);
          }
        }
        // Shift+Enter: newline (default behavior)
        // Plain Enter: newline (default behavior)
      }
    });
    
    // Initial resize
    autoResize();
  }
  
  // Auto-initialize on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
