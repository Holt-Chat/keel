class Router:
    """Minimal Flask-Blueprint-like collector of routes for later registration."""
    def __init__(self, name):
        self.name=name
        self.routes=[]
    def route(self, rule, methods=None, **options):
        methods=methods or ["GET"]
        def decorator(f):
            self.routes.append((rule, list(methods), f))
            return f
        return decorator
