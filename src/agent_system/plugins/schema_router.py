"""Schema-based Router Generator for Web Plugins

Automatically generates FastAPI routes from schema.yaml endpoint definitions.
This eliminates manual route definition duplication between schema and code.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional
from fastapi import APIRouter
from fastapi.responses import Response, HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)


class SchemaRouterGenerator:
    """Generate FastAPI routes from schema.yaml endpoint definitions.
    
    This class bridges the gap between schema.yaml documentation and actual
    FastAPI route implementation, ensuring consistency and reducing duplication.
    
    Example schema.yaml:
        web_ui:
          endpoints:
            - path: "/dashboard"
              method: "GET"
              handler: "get_dashboard"  # Method name in handler class
              response_type: "html"
              description: "Main dashboard"
    
    Example usage:
        class MyWebEndpoints:
            def __init__(self, plugin_name: str, schema: dict):
                self.plugin_name = plugin_name
                self.router_gen = SchemaRouterGenerator(
                    plugin_name=plugin_name,
                    schema=schema,
                    handler_class=self
                )
            
            def get_web_router(self) -> APIRouter:
                return self.router_gen.generate_router()
            
            async def get_dashboard(self, request: Request):
                # Handler implementation
                return HTMLResponse("<h1>Dashboard</h1>")
    """
    
    def __init__(
        self,
        plugin_name: str,
        schema: Dict[str, Any],
        handler_class: Any,
        prefix: Optional[str] = None
    ):
        """Initialize router generator.
        
        Args:
            plugin_name: Name of the plugin
            schema: Loaded schema.yaml data (with Jinja2 templates already rendered)
            handler_class: Instance containing handler methods
            prefix: Optional URL prefix (default: /plugins/{plugin_name})
        """
        self.plugin_name = plugin_name
        self.schema = schema
        self.handler_class = handler_class
        self.prefix = prefix or f"/plugins/{plugin_name}"
        
        # Extract endpoint definitions from schema
        web_ui = schema.get('web_ui', {})
        self.endpoints = web_ui.get('endpoints', [])
        
        logger.debug(
            f"SchemaRouterGenerator initialized for {plugin_name} "
            f"with {len(self.endpoints)} endpoints"
        )
    
    def generate_router(self) -> APIRouter:
        """Generate FastAPI router from schema endpoint definitions.
        
        Returns:
            APIRouter with all routes registered according to schema
        
        Raises:
            RuntimeError: If handler method is missing or schema is invalid
        """
        router = APIRouter(prefix=self.prefix)
        
        if not self.endpoints:
            logger.warning(
                f"No endpoints defined in schema for {self.plugin_name}. "
                "Add 'web_ui.endpoints' section to schema.yaml or use manual routing."
            )
            return router
        
        for endpoint_def in self.endpoints:
            self._register_endpoint(router, endpoint_def)
        
        logger.info(
            f"Generated {len(self.endpoints)} routes for {self.plugin_name}"
        )
        return router
    
    def _register_endpoint(
        self,
        router: APIRouter,
        endpoint_def: Dict[str, Any]
    ) -> None:
        """Register a single endpoint on the router.
        
        Args:
            router: FastAPI router to register on
            endpoint_def: Endpoint definition from schema
        
        Raises:
            RuntimeError: If handler method is missing or invalid
        """
        # Extract endpoint configuration
        path = endpoint_def.get('path', '/')
        method = endpoint_def.get('method', 'GET').upper()
        handler_name = endpoint_def.get('handler')
        response_type = endpoint_def.get('response_type', 'json')
        description = endpoint_def.get('description', '')
        
        # Validate configuration
        if not handler_name:
            raise RuntimeError(
                f"Missing 'handler' in endpoint definition for {path} "
                f"in {self.plugin_name} schema. Add 'handler: method_name' to endpoint."
            )
        
        # Get handler method from handler class
        if not hasattr(self.handler_class, handler_name):
            raise RuntimeError(
                f"Handler method '{handler_name}' not found in {self.handler_class.__class__.__name__} "
                f"for endpoint {method} {path} in {self.plugin_name}. "
                f"Add method: async def {handler_name}(self, request: Request) -> Response"
            )
        
        handler_method = getattr(self.handler_class, handler_name)
        
        # Determine response class
        response_class = self._get_response_class(response_type)
        
        # Create route decorator based on HTTP method
        route_decorator = self._get_route_decorator(router, method)
        
        # Register the route
        route_decorator(
            path,
            response_class=response_class,
            description=description,
            name=handler_name
        )(handler_method)
        
        logger.debug(
            f"Registered route: {method} {self.prefix}{path} -> {handler_name}()"
        )
    
    def _get_response_class(self, response_type: str):
        """Get FastAPI response class for given type.
        
        Args:
            response_type: Type string ('html', 'json', 'text', 'response')
        
        Returns:
            FastAPI response class
        """
        response_classes = {
            'html': HTMLResponse,
            'json': JSONResponse,
            'text': Response,
            'response': Response
        }
        return response_classes.get(response_type.lower(), Response)
    
    def _get_route_decorator(self, router: APIRouter, method: str) -> Callable:
        """Get route decorator function for HTTP method.
        
        Args:
            router: FastAPI router
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
        
        Returns:
            Route decorator function
        
        Raises:
            ValueError: If HTTP method is not supported
        """
        method_map = {
            'GET': router.get,
            'POST': router.post,
            'PUT': router.put,
            'DELETE': router.delete,
            'PATCH': router.patch
        }
        
        if method not in method_map:
            raise ValueError(
                f"Unsupported HTTP method '{method}' in {self.plugin_name} schema. "
                f"Supported methods: {list(method_map.keys())}"
            )
        
        return method_map[method]


def create_schema_router(
    plugin_name: str,
    schema: Dict[str, Any],
    handler_class: Any,
    prefix: Optional[str] = None
) -> APIRouter:
    """Convenience function to create router from schema in one call.
    
    Args:
        plugin_name: Name of the plugin
        schema: Loaded schema.yaml data
        handler_class: Instance containing handler methods
        prefix: Optional URL prefix
    
    Returns:
        Generated FastAPI router
    
    Example:
        router = create_schema_router(
            plugin_name="my_plugin",
            schema=self.get_schema_data(),
            handler_class=self
        )
    """
    generator = SchemaRouterGenerator(
        plugin_name=plugin_name,
        schema=schema,
        handler_class=handler_class,
        prefix=prefix
    )
    return generator.generate_router()
