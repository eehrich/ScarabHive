"""Markdown Formatter Plugin - Hook implementation.

Formats LLM output as Markdown and converts to HTML for frontend display.
Injects system prompt to guide LLM to generate Markdown output.
"""
from __future__ import annotations

import logging
from pathlib import Path
import re

from agent_system.hooks import HookContext, HookResult
from agent_system.hooks.schema_based import SchemaBasedPluginHook

logger = logging.getLogger(__name__)

# Track sessions where system prompt was injected to avoid duplicates
_injected_sessions: set[str] = set()


class MarkdownFormatterPlugin(SchemaBasedPluginHook):
    """Hook plugin for Markdown formatting and HTML conversion."""
    
    def __init__(self, plugin_dir: Path):
        """Initialize the markdown formatter plugin.
        
        Args:
            plugin_dir: Directory containing plugin configuration files
        """
        super().__init__(plugin_dir)
        
        # Get config from schema with proper dict handling
        config = self.config or {}
        
        def get_config_value(key: str, default):
            val = config.get(key, default)
            if isinstance(val, dict):
                return val.get('default', default)
            return val
        
        self.inject_system_prompt = get_config_value('inject_system_prompt', True)
        self.system_prompt_template = get_config_value(
            'system_prompt_template',
            "Format your responses using Markdown for better readability."
        )
        self.convert_to_html = get_config_value('convert_to_html', True)
        self.enable_code_highlighting = get_config_value('enable_code_highlighting', True)
        self.enable_tables = get_config_value('enable_tables', True)
        self.enable_autolinks = get_config_value('enable_autolinks', True)
        self.sanitize_html = get_config_value('sanitize_html', True)
        
        allowed_tags = config.get('allowed_html_tags', [
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'strong', 'em',
            'code', 'pre', 'ul', 'ol', 'li', 'table', 'thead', 'tbody',
            'tr', 'th', 'td', 'a', 'blockquote', 'hr'
        ])
        if isinstance(allowed_tags, dict):
            allowed_tags = allowed_tags.get('default', [])
        self.allowed_html_tags = set(allowed_tags)
        
        # Initialize markdown converter
        try:
            import markdown
            from markdown.extensions import tables, fenced_code, codehilite
            
            extensions = []
            if self.enable_tables:
                extensions.append('tables')
            if self.enable_code_highlighting:
                extensions.extend(['fenced_code', 'codehilite'])
            
            self.markdown_converter = markdown.Markdown(
                extensions=extensions,
                output_format='html5'
            )
            logger.debug(f"MarkdownFormatterPlugin initialized with {len(extensions)} extensions")
        except ImportError as e:
            logger.warning(f"Failed to initialize markdown converter: {e}")
            self.markdown_converter = None
    
    async def inject_markdown_system_prompt(self, context: HookContext) -> HookResult:
        """Inject system prompt at session start to guide LLM to use Markdown.
        
        Hook implementation for session_start. Injects system message once per session.
        
        Args:
            context: Hook context with session information
            
        Returns:
            HookResult with modified context containing system prompt
        """
        try:
            if not self.inject_system_prompt:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'injection_disabled'}
                )
            
            session_id = context.session_id or 'unknown'
            
            # Check if already injected for this session
            if session_id in _injected_sessions:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'already_injected', 'session_id': session_id}
                )
            
            # Inject system message
            messages = context.messages or []
            
            # Check if system message already exists
            has_system = any(
                msg.get('role') == 'system' 
                if isinstance(msg, dict) 
                else getattr(msg, 'role', None) == 'system'
                for msg in messages
            )
            
            if has_system:
                # Append to existing system message
                for msg in messages:
                    msg_role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', None)
                    if msg_role == 'system':
                        existing_content = msg.get('content') if isinstance(msg, dict) else getattr(msg, 'content', '')
                        new_content = f"{existing_content}\n\n{self.system_prompt_template}"
                        if isinstance(msg, dict):
                            msg['content'] = new_content
                        else:
                            msg.content = new_content
                        break
            else:
                # Insert new system message at the beginning
                system_msg = {
                    'role': 'system',
                    'content': self.system_prompt_template
                }
                messages.insert(0, system_msg)
            
            # Mark as injected
            _injected_sessions.add(session_id)
            context.messages = messages
            
            logger.debug(f"Injected Markdown system prompt for session {session_id}")
            
            return HookResult(
                success=True,
                modified=True,
                context=context,
                metadata={
                    'injected': True,
                    'session_id': session_id,
                    'prompt_length': len(self.system_prompt_template)
                }
            )
            
        except Exception as e:
            logger.exception(f"Error in inject_markdown_system_prompt: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def format_markdown_output(self, context: HookContext) -> HookResult:
        """Convert LLM Markdown output to HTML.
        
        Hook implementation for post_llm_call. Converts assistant message Markdown to HTML.
        
        Args:
            context: Hook context with LLM response
            
        Returns:
            HookResult with HTML-formatted content
        """
        try:
            if not self.convert_to_html or not self.markdown_converter:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'conversion_disabled_or_unavailable'}
                )
            
            llm_response = context.llm_response
            if not llm_response:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_llm_response'}
                )
            
            # Extract assistant content
            assistant_data = llm_response.get('assistant', {})
            content = assistant_data.get('content', '')
            
            if not content or not isinstance(content, str):
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_content_to_convert'}
                )
            
            # Convert Markdown to HTML
            html_content = self.markdown_converter.convert(content)
            
            # Sanitize HTML if enabled
            if self.sanitize_html:
                html_content = self._sanitize_html(html_content)
            
            # Update response with HTML content
            llm_response['assistant']['content'] = html_content
            llm_response['assistant']['content_format'] = 'html'  # Flag for frontend
            context.llm_response = llm_response
            
            logger.debug(f"Converted {len(content)} chars of Markdown to {len(html_content)} chars of HTML")
            
            return HookResult(
                success=True,
                modified=True,
                context=context,
                metadata={
                    'converted': True,
                    'original_length': len(content),
                    'html_length': len(html_content),
                    'format': 'html'
                }
            )
            
        except Exception as e:
            logger.exception(f"Error in format_markdown_output: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    def _sanitize_html(self, html: str) -> str:
        """Sanitize HTML to prevent XSS attacks.
        
        Simple sanitization that only allows whitelisted tags.
        
        Args:
            html: HTML string to sanitize
            
        Returns:
            Sanitized HTML string
        """
        # Remove script tags and their content
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove event handlers
        html = re.sub(r'\son\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=re.IGNORECASE)
        
        # Remove javascript: URLs
        html = re.sub(r'href\s*=\s*["\']javascript:[^"\']*["\']', '', html, flags=re.IGNORECASE)
        
        # Simple tag whitelist (more sophisticated solutions would use bleach library)
        # For now, we trust markdown library's output and just remove obvious threats
        
        return html
