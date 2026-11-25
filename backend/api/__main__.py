"""Start the front end: python -m backend.api  (then open http://127.0.0.1:8000)"""
import webbrowser

import uvicorn

if __name__ == "__main__":
    url = "http://127.0.0.1:8000"
    print(f"Ground-Stop Co-Pilot is running at {url}  (press Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run("backend.api.app:app", host="127.0.0.1", port=8000, log_level="warning")
