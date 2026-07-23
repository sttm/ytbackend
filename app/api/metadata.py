from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import TrackMetadataLookupRequest, TrackMetadataSubmitRequest
from app.services.track_metadata import lookup_track_metadata, submit_track_metadata

router = APIRouter()


@router.post("/api/metadata/lookup")
def metadata_lookup(payload: TrackMetadataLookupRequest, db: Session = Depends(get_db)):
    return lookup_track_metadata(db, payload.model_dump())


@router.post("/api/metadata/submit")
def metadata_submit(payload: TrackMetadataSubmitRequest, db: Session = Depends(get_db)):
    try:
        return submit_track_metadata(db, payload.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
