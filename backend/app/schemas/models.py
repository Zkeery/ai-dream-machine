"""用户仅选择已公开的模型 ID，供应商与鉴权配置仍由服务端管理。"""
from pydantic import BaseModel, ConfigDict, Field, StrictStr


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr | None = Field(None, min_length=1, max_length=100)
    image: StrictStr | None = Field(None, min_length=1, max_length=100)
    video_first_frame: StrictStr | None = Field(None, min_length=1, max_length=100)
    video_start_end: StrictStr | None = Field(None, min_length=1, max_length=100)
    video_reference: StrictStr | None = Field(None, min_length=1, max_length=100)
    video_speech: StrictStr | None = Field(None, min_length=1, max_length=100)


class SessionModelsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_selection: ModelSelection
