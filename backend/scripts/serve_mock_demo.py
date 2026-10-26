"""Local recording server: demo clients, guarded network and isolated data."""
from demo_fixtures import configure, make_orchestrator

configure()
from app.main import app
from app.api.deps import get_orchestrator

orchestrator = make_orchestrator()
app.dependency_overrides[get_orchestrator] = lambda: orchestrator

if __name__ == "__main__":
    import uvicorn
    print("本地录屏素材服务：http://127.0.0.1:3030 · Mock 隔离 · 无模型调用")
    uvicorn.run(app, host="127.0.0.1", port=8030, access_log=False)
