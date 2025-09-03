import uvicorn

# PUBLIC_INTERFACE
def run():
    """Run the FastAPI app with Uvicorn for local development."""
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )

if __name__ == "__main__":
    run()
