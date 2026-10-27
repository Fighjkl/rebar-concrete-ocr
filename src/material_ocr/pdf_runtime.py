"""PDFium calls share one process lock across HTTP rendering and diagnostics."""
import threading


PDF_RENDER_LOCK = threading.RLock()
