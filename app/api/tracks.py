from fastapi import APIRouter

router = APIRouter()


@router.get("/api/tracks")
@router.get("/api/tracks/search")
def tracks_moved():
    return moved_response()


@router.post("/api/tracks/usage")
def track_usage_moved():
    return {
        "status": "ignored",
        "reason": "track catalog usage moved to ProducersCenter catalog service",
        "catalog_service": "producerscenter-admin",
    }


@router.delete("/api/tracks/{provider}/{media_id}")
@router.delete("/api/tracks")
def tracks_delete_moved():
    return moved_response()


def moved_response():
    return {
        "status": "moved",
        "reason": "track catalog, filters, TOP, and metadata promotion moved out of yt-dlp proxy backend",
        "catalog_service": "producerscenter-admin",
    }
