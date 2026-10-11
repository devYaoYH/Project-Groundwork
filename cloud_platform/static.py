"""Serve the original built researcher UI and narrowly allowed replay assets."""
import os
from pathlib import Path
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse


def static_response(request):
    relative = request.url.path.lstrip('/') or 'index.html'
    if relative.startswith(('api/', 'auth/', 'internal/')):
        raise HTTPException(404, 'Not found')
    root = Path(os.getenv('PLATFORM_STATIC_DIR', Path.cwd() / 'web/out')).resolve()
    if relative.startswith(('environment-replays/', 'environment-assets/')):
        namespace, _, tail = relative.partition('/')
        directory, _, relative = tail.partition('/')
        workspace = Path(os.getenv('PLATFORM_WORKSPACE', Path.cwd())).resolve()
        games = (workspace / 'games').resolve()
        game = (games / directory).resolve()
        # Directory identity is confined to checked-in game folders. Never
        # publish source/config files through a broad environment-assets mount.
        if game.parent != games or not game.is_dir():
            raise HTTPException(404, 'Not found')
        if namespace == 'environment-replays':
            root = (game / 'replay').resolve()
        elif directory == 'negotiation' and relative.startswith('webapp/static/'):
            root = (game / 'webapp/static').resolve()
            relative = relative.removeprefix('webapp/static/')
        elif directory == 'calendar' and relative.startswith('tasks/'):
            root = (game / 'tasks').resolve()
            relative = relative.removeprefix('tasks/')
        else:
            raise HTTPException(404, 'Not found')
        relative = relative or 'index.html'
    if request.url.path.startswith(("/environment-assets/", "/environment-replays/")):
        if Path(relative).suffix and Path(relative).suffix.lower() not in {".html", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2"}:
            raise HTTPException(404, "Not found")
    path = (root / relative).resolve()
    if path.is_dir():
        path = (path / 'index.html').resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, 'Not found')
    return FileResponse(path)
