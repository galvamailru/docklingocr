import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from docling.document_converter import DocumentConverter
from docling_core.types.doc import PictureItem, TableItem
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Docling PDF OCR API (OCR2)", version="2.0.0")

# Serve simple UI
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


def _bbox_to_list(bbox: Any) -> Optional[List[float]]:
    """Safely normalize bbox to list if present."""
    if bbox is None:
        return None
    if isinstance(bbox, (list, tuple)):
        return [float(x) for x in bbox]
    # Try common attribute names used by Docling's BoundingBox
    # 1) x0, y0, x1, y1
    coords_xy: List[float] = []
    for attr in ("x0", "y0", "x1", "y1"):
        if hasattr(bbox, attr):
            coords_xy.append(float(getattr(bbox, attr)))
    if coords_xy:
        return coords_xy

    # 2) left, top, right, bottom
    coords_ltrb: List[float] = []
    for attr in ("left", "top", "right", "bottom"):
        if hasattr(bbox, attr):
            coords_ltrb.append(float(getattr(bbox, attr)))
    if coords_ltrb:
        return coords_ltrb

    return None


def _extract_page_and_bbox(element: Any) -> tuple[Optional[int], Optional[List[float]]]:
    """
    Docling обычно хранит координаты/страницу в provenance: element.prov[0].bbox, element.prov[0].page_no.
    Возвращаем (page_no, bbox_list).
    """
    prov = getattr(element, "prov", None)
    if isinstance(prov, list) and prov:
        first = prov[0]
        page_no = getattr(first, "page_no", None)
        bbox = getattr(first, "bbox", None)
        bbox_list = _bbox_to_list(bbox)
        if bbox_list is not None or page_no is not None:
            return (int(page_no) if page_no is not None else None, bbox_list)

    # fallback: element-level bbox (если есть)
    return (None, _bbox_to_list(getattr(element, "bbox", None)))


def _iterate_items(doc: Any) -> Iterable[Tuple[Any, int]]:
    """Compatible wrapper around Docling's iterate_items()."""
    if hasattr(doc, "iterate_items"):
        yield from doc.iterate_items()
    else:
        # fallback: no structure information
        for el in getattr(doc, "elements", []):
            yield el, 0


def _extract_objects(doc: Any) -> List[Dict[str, Any]]:
    """
    Extract objects from Docling document:
    - tables (TableItem)
    - images (PictureItem)
    - text blocks (everything остальное с текстом)
    """
    objects: List[Dict[str, Any]] = []

    for element, _level in _iterate_items(doc):
        page_no, bbox = _extract_page_and_bbox(element)

        if isinstance(element, TableItem):
            kind = "table"
            # Try to get some textual representation of the table
            text = ""
            export_md = getattr(element, "export_to_markdown", None)
            if callable(export_md):
                try:
                    text = export_md(doc=doc)
                except Exception:  # noqa: BLE001
                    text = ""
        elif isinstance(element, PictureItem):
            kind = "image"
            text = getattr(element, "caption", "") or ""
        else:
            # Treat as text block
            kind = getattr(element, "category", None) or "text"
            text = getattr(element, "text", None) or getattr(element, "content", None) or ""

        objects.append({"type": str(kind), "page": page_no, "bbox": bbox, "text": text})

    # Fallback to exported dict if list is still empty
    if not objects and hasattr(doc, "export_to_dict"):
        data = doc.export_to_dict()
        blocks = data.get("blocks") or data.get("elements") or []
        for block in blocks:
            objects.append(
                {
                    "type": block.get("category") or block.get("type") or "unknown",
                    "page": block.get("page_no") or block.get("page"),
                    "bbox": block.get("bbox"),
                    "text": block.get("text", ""),
                }
            )

    return objects


def convert_pdf(pdf_path: Path) -> Dict[str, Any]:
    """
    Convert a PDF to text and structured objects using Docling.
    """
    converter = DocumentConverter()
    result = converter.convert(pdf_path)
    doc = result.document
    text = doc.export_to_text()
    objects = _extract_objects(doc)
    return {"text": text, "objects": objects}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    # redirect to static UI
    return JSONResponse(
        status_code=307,
        headers={"Location": "/static/index.html"},
        content={},
    )


@app.post("/parse")
async def parse_pdf(file: UploadFile = File(...)):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        return JSONResponse(
            status_code=400,
            content={"error": "File must be a PDF"},
        )

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = Path(tmp.name)

        result = convert_pdf(tmp_path)
        return {"filename": file.filename, **result}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to process PDF: {exc}"},
        )
    finally:
        if "tmp_path" in locals() and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

