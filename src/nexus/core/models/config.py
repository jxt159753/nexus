"""Owner-controlled model limits; credentials are never configuration fields."""

from datetime import date

from pydantic import Field, StrictBool, StrictInt, model_validator

from nexus.contracts.base import StrictContract


class ModelConfig(StrictContract):
    enabled: StrictBool = False
    daily_cost_microunits: StrictInt = Field(default=1_000_000, ge=0, le=1_000_000)
    task_cost_microunits: StrictInt = Field(default=50_000, ge=0, le=50_000)
    daily_calls: StrictInt = Field(default=100, ge=0, le=100)
    max_output_tokens: StrictInt = Field(default=512, ge=32, le=512)
    chat_enabled: StrictBool = False
    web_enabled: StrictBool = False
    chat_images_enabled: StrictBool = False
    web_request_cost_microunits: StrictInt = Field(default=750, ge=750, le=50_000)
    chat_max_output_tokens: StrictInt = Field(default=2048, ge=32, le=2048)
    chat_input_bytes: StrictInt = Field(default=32768, ge=8192, le=32768)
    # Adapter capacity and local admission budget are distinct. deepseek-flash
    # advertises a 1M context; costs/transport retain the stricter local limits.
    chat_context_window_tokens: StrictInt = Field(default=1_000_000, ge=4096, le=1_000_000)
    chat_input_tokens: StrictInt = Field(default=32768, ge=1024, le=32768)
    chat_compaction_threshold_percent: StrictInt = Field(default=80, ge=30, le=95)
    chat_compaction_retain_percent: StrictInt = Field(default=16, ge=1, le=25)
    chat_compaction_headroom_tokens: StrictInt = Field(default=512, ge=0, le=4096)
    timeout_seconds: StrictInt = Field(default=30, ge=1, le=30)
    # USD microunits per million tokens. Peak cache-miss rates are conservative.
    input_price: StrictInt = Field(default=300_000, ge=1)
    output_price: StrictInt = Field(default=1_200_000, ge=1)
    price_valid_until: date = date(2026, 10, 25)

    @model_validator(mode="after")
    def check_reservation(self):
        if self.chat_context_window_tokens <= self.chat_max_output_tokens + self.chat_compaction_headroom_tokens:
            raise ValueError("context capacity must leave room for input, output and compaction")
        worst = (16384*self.input_price + self.max_output_tokens*self.output_price + 999999)//1000000
        if self.enabled and self.task_cost_microunits and worst > self.task_cost_microunits:
            raise ValueError("token price ceiling exceeds request budget")
        chat_worst = (32768*self.input_price + self.chat_max_output_tokens*self.output_price + 999999)//1000000
        if self.enabled and self.chat_enabled and self.task_cost_microunits and chat_worst > self.task_cost_microunits:
            raise ValueError("chat token price ceiling exceeds request budget")
        return self
