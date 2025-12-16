"""Markdown Formatter Plugin - Hook implementation.

Formats LLM output as Markdown and converts to HTML for frontend display.
Injects system prompt to guide LLM to generate Markdown output.
"""
from __future__ import annotations

import logging
from pathlib import Path
import re
from dataclasses import replace

from agent_system.hooks import HookContext, HookResult
from agent_system.hooks.schema_based import SchemaBasedPluginHook
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)


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
            
            extensions = []
            if self.enable_tables:
                extensions.append('tables')
            if self.enable_code_highlighting:
                # Use fenced_code with Prism.js-compatible class names
                # Note: Do NOT use 'codehilite' - it generates incompatible CSS classes
                extensions.append('fenced_code')
            
            # Add nl2br to convert newlines to <br> tags
            # This ensures list items appear on separate lines
            extensions.append('nl2br')
            
            # Configure fenced_code to use 'language-' prefix for Prism.js
            extension_configs = {
                'fenced_code': {
                    'lang_prefix': 'language-'
                }
            }
            
            self.markdown_converter = markdown.Markdown(
                extensions=extensions,
                extension_configs=extension_configs,
                output_format='html5'
            )
            logger.debug(f"MarkdownFormatterPlugin initialized with {len(extensions)} extensions")
        except ImportError as e:
            logger.warning(f"Failed to initialize markdown converter: {e}")
            self.markdown_converter = None
    
    async def inject_markdown_system_prompt(self, context: HookContext) -> HookResult:
        """Inject system prompt before each LLM call to guide LLM to use Markdown.
        
        Hook implementation for pre_llm_call. Injects system message temporarily
        for the LLM call without persisting to the session storage.
        
        Args:
            context: Hook context with messages for LLM call
            
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
            
            # Inject system message before every LLM call
            messages = context.messages or []
            
            # Check if system message already exists
            has_system = any(
                msg.get('role') == 'system' 
                if isinstance(msg, dict) 
                else getattr(msg, 'role', None) == 'system'
                for msg in messages
            )
            
            # Track if we actually modified anything
            was_modified = False
            
            if has_system:
                # Append to first system message only
                modified_messages = []
                already_appended = False
                for msg in messages:
                    msg_role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', None)
                    if msg_role == 'system' and not already_appended:
                        existing_content = msg.get('content') if isinstance(msg, dict) else getattr(msg, 'content', '')
                        # Check if already injected to avoid duplication
                        if self.system_prompt_template in existing_content:
                            modified_messages.append(msg)
                            continue
                        new_content = f"{existing_content}\n\n{self.system_prompt_template}"
                        if isinstance(msg, dict):
                            modified_msg = {**msg, 'content': new_content}
                        else:
                            # ChatMessage - use model_copy
                            modified_msg = msg.model_copy(update={'content': new_content})
                        modified_messages.append(modified_msg)
                        was_modified = True
                        already_appended = True
                    else:
                        modified_messages.append(msg)
                messages = modified_messages
            else:
                # Insert new system message at the beginning
                system_msg = ChatMessage(
                    role='system',
                    content=self.system_prompt_template
                )
                messages = [system_msg] + list(messages)
                was_modified = True
            
            # If nothing was modified, return original context
            if not was_modified:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'already_present'}
                )
            
            # Return modified context with new messages list
            modified_context = replace(context, messages=messages)
            
            # Debug: log the modified messages to verify injection worked
            system_msg_content = ""
            if messages:
                for msg in messages:
                    msg_role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', None)
                    if msg_role == 'system':
                        system_msg_content = msg.get('content') if isinstance(msg, dict) else getattr(msg, 'content', '')
                        break
            logger.info(f"[MarkdownFormatter] Injected prompt. System message now {len(system_msg_content)} chars. Contains markdown template: {self.system_prompt_template[:50] in system_msg_content}")
            
            return HookResult(
                success=True,
                modified=True,
                context=modified_context,
                metadata={
                    'injected': True,
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
        """Convert LLM Markdown output to requested format.
        
        Hook implementation for format_output. Converts Markdown text to
        the requested format (HTML, ANSI, etc.) for rendering without
        modifying stored messages.
        
        Args:
            context: Hook context with output string and output_format
            
        Returns:
            HookResult with formatted content and content_format metadata
        """
        try:
            # Get target format from context
            target_format = context.output_format or 'text'
            
            logger.info(f"format_markdown_output called: target_format={target_format}, output_length={len(context.output) if context.output else 0}")
            
            # Get output from context
            output = context.output
            if not output or not isinstance(output, str):
                logger.warning(f"No valid output in context: output={output}, type={type(output)}")
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'content_format': 'text', 'reason': 'no_output'}
                )
            
            # Extract markdown content if wrapped in ```markdown``` code block
            # This handles cases where LLM wraps markdown in a code block
            markdown_content = self._extract_markdown_content(output)
            if markdown_content != output:
                logger.debug(f"Extracted markdown from code block wrapper (original: {len(output)} chars, extracted: {len(markdown_content)} chars)")
                output = markdown_content
            
            # Handle different target formats
            if target_format == 'html':
                if not self.convert_to_html or not self.markdown_converter:
                    return HookResult(
                        success=True,
                        modified=False,
                        context=context,
                        metadata={'content_format': 'text'}
                    )
                
                # Convert Markdown to HTML
                html_content = self.markdown_converter.convert(output)
                
                # Fix list rendering: ensure lists have proper line breaks
                # Markdown requires blank line before lists, but LLMs often forget this
                html_content = self._fix_list_formatting(html_content)
                
                # Sanitize HTML if enabled
                if self.sanitize_html:
                    html_content = self._sanitize_html(html_content)
                
                # Update context with formatted output
                context.output = html_content
                
                logger.debug(f"Converted {len(output)} chars of Markdown to {len(html_content)} chars of HTML")
                
                return HookResult(
                    success=True,
                    modified=True,
                    context=context,
                    metadata={
                        'content_format': 'html',
                        'converted': True,
                        'original_length': len(output),
                        'html_length': len(html_content)
                    }
                )
            
            elif target_format == 'ansi':
                # For ANSI output, prepare content for Rich Console rendering in CLI
                # Input may be Markdown (from LLM) OR HTML (from earlier format_output hook with html target)
                
                logger.info(f"Preparing content for ANSI rendering (length: {len(output)})")
                
                # Check if input is HTML (from earlier format_output hook that converted for web/storage)
                if output.strip().startswith('<') and ('</p>' in output or '</h1>' in output or '</h2>' in output):
                    logger.info("Detected HTML input for ANSI, converting back to Markdown for Rich rendering")
                    try:
                        # Convert HTML back to Markdown so Rich Console can render it properly
                        from markdownify import markdownify as md_convert
                        markdown_content = md_convert(output, heading_style="ATX")
                        
                        logger.debug(f"HTML->Markdown conversion: {len(output)} -> {len(markdown_content)} chars")
                        
                        return HookResult(
                            success=True,
                            modified=True,
                            context=replace(context, output=markdown_content),
                            metadata={
                                'content_format': 'ansi',
                                'converted_from': 'html',
                                'original_length': len(output),
                                'markdown_length': len(markdown_content),
                                'render_with_rich': True
                            }
                        )
                    except ImportError:
                        logger.warning("markdownify not available for HTML->Markdown conversion, stripping HTML tags")
                        # Fallback: strip HTML tags to get plain text
                        import re
                        text_content = re.sub(r'<[^>]+>', '', output).strip()
                        return HookResult(
                            success=True,
                            modified=True,
                            context=replace(context, output=text_content),
                            metadata={
                                'content_format': 'ansi',
                                'converted_from': 'html_stripped',
                                'original_length': len(output),
                                'text_length': len(text_content)
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to convert HTML for ANSI: {e}")
                        # Return as-is and let CLI handle it
                
                # Input is already Markdown - return as-is for Rich Console rendering
                logger.debug(f"Markdown content ready for ANSI rendering (length: {len(output)})")
                
                return HookResult(
                    success=True,
                    modified=False,
                    context=replace(context, output=output),
                    metadata={
                        'content_format': 'ansi',
                        'original_length': len(output),
                        'render_with_rich': True  # Signal to CLI to use Rich Console
                    }
                )
            
            elif target_format == 'text':
                # Convert to plain text
                # If input is HTML, strip tags; otherwise return as-is
                text_content = output
                
                if output.strip().startswith('<') and ('<p>' in output or '<h1>' in output or '<pre>' in output):
                    logger.info("Detected HTML input for text output, stripping HTML tags")
                    try:
                        from markdownify import markdownify as md_convert
                        import re
                        
                        # Convert HTML to Markdown first
                        markdown_content = md_convert(output, heading_style="ATX")
                        
                        # Then strip markdown formatting to get plain text
                        # Remove markdown headers
                        text_content = re.sub(r'^#+\s+', '', markdown_content, flags=re.MULTILINE)
                        # Remove bold/italic
                        text_content = re.sub(r'\*\*([^*]+)\*\*', r'\1', text_content)
                        text_content = re.sub(r'\*([^*]+)\*', r'\1', text_content)
                        text_content = re.sub(r'__([^_]+)__', r'\1', text_content)
                        text_content = re.sub(r'_([^_]+)_', r'\1', text_content)
                        # Remove inline code backticks
                        text_content = re.sub(r'`([^`]+)`', r'\1', text_content)
                        # Remove links but keep text
                        text_content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text_content)
                        
                        logger.info(f"HTML->Text conversion done (text length: {len(text_content)})")
                        
                    except ImportError:
                        logger.warning("markdownify not available for HTML->text conversion")
                    except Exception as e:
                        logger.warning(f"Error converting HTML to text: {e}")
                
                # Return text content
                return HookResult(
                    success=True,
                    modified=(text_content != output),
                    context=replace(context, output=text_content) if text_content != output else context,
                    metadata={
                        'content_format': 'text',
                        'converted': text_content != output,
                        'original_length': len(output),
                        'text_length': len(text_content)
                    }
                )
            
            else:
                # For 'markdown' or unknown formats, return as-is
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'content_format': target_format or 'text'}
                )
            
        except Exception as e:
            logger.exception(f"Error in format_markdown_output: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                metadata={'content_format': 'text'},
                error=str(e)
            )
    
    def _extract_markdown_content(self, text: str) -> str:
        """Extract markdown content from code block wrapper if present.
        
        Handles cases where LLM wraps markdown output in ```markdown``` code block.
        Also handles cases where markdown content is not wrapped.
        
        Args:
            text: Input text that may be wrapped in ```markdown``` block
            
        Returns:
            Extracted markdown content or original text
        """
        # Pattern to match ```markdown ... ``` or ```md ... ```
        # Use DOTALL flag to match newlines within the block
        pattern = r'^```(?:markdown|md)\s*\n(.*?)\n```\s*$'
        match = re.match(pattern, text.strip(), re.DOTALL)
        
        if match:
            # Extract content from code block
            return match.group(1)
        
        # No wrapper found, treat entire text as markdown
        return text
    
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
    
    def _fix_list_formatting(self, html: str) -> str:
        """Fix inline list items that should be on separate lines.
        
        When markdown lists are not properly separated by blank lines,
        they get rendered inline. This fixes that by ensuring list items
        appear on separate lines.
        
        Args:
            html: HTML content that may contain inline list items
            
        Returns:
            HTML with properly formatted lists
        """
        # Pattern: text followed by list items rendered inline (without proper <ul>/<ol>)
        # Example: "<p>Text - Item 1 - Item 2 - Item 3</p>"
        # Should be: "<p>Text</p><ul><li>Item 1</li><li>Item 2</li><li>Item 3</li></ul>"
        
        # Find paragraphs containing multiple "- " or "• " list markers
        def fix_inline_list(match):
            content = match.group(1)
            
            # Check if this looks like an inline list (multiple - or • on one line)
            if content.count(' - ') >= 2 or content.count(' • ') >= 2:
                # Split by list markers
                parts = re.split(r'\s[-•]\s', content)
                
                # First part might be intro text
                intro = parts[0].strip()
                items = [p.strip() for p in parts[1:] if p.strip()]
                
                # Build proper list HTML
                html_parts = []
                if intro:
                    html_parts.append(f'<p>{intro}</p>')
                if items:
                    html_parts.append('<ul>')
                    for item in items:
                        html_parts.append(f'<li>{item}</li>')
                    html_parts.append('</ul>')
                
                return ''.join(html_parts)
            
            # Not an inline list, return as-is
            return match.group(0)
        
        # Apply fix to paragraphs
        html = re.sub(r'<p>(.*?)</p>', fix_inline_list, html, flags=re.DOTALL)
        
        return html
