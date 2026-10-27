"""Owner-controlled model limits; credentials are never configuration fields."""

from datetime import date

from pydantic import Field, StrictBool, StrictInt, model_validator

from nexus.contracts.base import StrictContract

CHAT_MAX_OUTPUT_TOKENS = 256_000
CHAT_MAX_TEXT_BYTES = 1_048_576
CHAT_MAX_INPUT_BYTES = 8 * 1024 * 1024


class ModelConfig(StrictContract):
    enabled: StrictBool = False
    cost_limits_enabled: StrictBool = True
    call_limits_enabled: StrictBool = True
    daily_cost_microunits: StrictInt = Field(default=1_000_000, ge=0, le=1_000_000)
    task_cost_microunits: StrictInt = Field(default=50_000, ge=0, le=50_000)
    daily_calls: StrictInt = Field(default=100, ge=0, le=100)
    max_output_tokens: StrictInt = Field(default=512, ge=32, le=512)
    chat_enabled: StrictBool = False
    chat_work_max_actions: StrictInt = Field(default=4, ge=1, le=16)
    web_enabled: StrictBool = False
    chat_images_enabled: StrictBool = False
    web_request_cost_microunits: StrictInt = Field(default=750, ge=750, le=50_000)
    # DSH provider default; each actual request is also capped by its reservation.
    chat_max_output_tokens: StrictInt = Field(default=CHAT_MAX_OUTPUT_TOKENS, ge=32, le=CHAT_MAX_OUTPUT_TOKENS)
    chat_input_bytes: StrictInt = Field(default=CHAT_MAX_INPUT_BYTES, ge=8192, le=CHAT_MAX_INPUT_BYTES)
    # DSH's provider window includes both input and reserved output. Explicit
    # owner limits may narrow it; transport must not impose the old 32/64 KiB cap.
    chat_context_window_tokens: StrictInt = Field(default=1_000_000, ge=4096, le=1_000_000)
    chat_input_tokens: StrictInt = Field(default=1_000_000, ge=1024, le=1_000_000)
    chat_compaction_threshold_percent: StrictInt = Field(default=80, ge=30, le=95)
    chat_compaction_retain_percent: StrictInt = Field(default=16, ge=1, le=25)
    chat_compaction_headroom_tokens: StrictInt = Field(default=65536, ge=0, le=65536)
    timeout_seconds: StrictInt = Field(default=30, ge=1, le=30)
    # USD microunits per million tokens. Peak cache-miss rates are conservative.
    input_price: StrictInt = Field(default=300_000, ge=1)
    output_price: StrictInt = Field(default=1_200_000, ge=1)
    price_valid_until: date = date(2026, 10, 25)

    def work_actions_remaining(self, used: int = 0) -> int:
        # A transport batch remains bounded; disabling quotas removes the total.
        return max(0, self.chat_work_max_actions - used) if self.call_limits_enabled else 4

    def chat_input_limit(self, output_tokens: int = 0) -> int:
        # The optional legacy monetary budget reserves 32768 input tokens.
        # With amount limits disabled, use the model's actual message budget.
        return min(self.chat_input_tokens, self.chat_context_window_tokens - output_tokens,
                   32768 if self.cost_limits_enabled else self.chat_context_window_tokens)

    def chat_output_limit(self, reservation: int | None = None, *, compact: bool = False) -> int:
        if not self.cost_limits_enabled:
            return min(self.chat_max_output_tokens, 1024 if compact else CHAT_MAX_OUTPUT_TOKENS)
        amount = self.task_cost_microunits if reservation is None else min(reservation, self.task_cost_microunits)
        # Keep the existing conservative input allowance and integer cost ceiling.
        affordable = (amount * 1_000_000 - 32768 * self.input_price) // self.output_price
        return max(0, min(self.chat_max_output_tokens, 1024 if compact else CHAT_MAX_OUTPUT_TOKENS, affordable))

    def model_reservation(self, *, input_tokens: int | None = None, output_tokens: int | None = None) -> int:
        if self.cost_limits_enabled:
            return self.task_cost_microunits
        output_tokens = self.chat_max_output_tokens if output_tokens is None else output_tokens
        input_tokens = self.chat_input_limit(output_tokens) if input_tokens is None else input_tokens
        return (input_tokens*self.input_price + output_tokens*self.output_price + 999999)//1_000_000

    @model_validator(mode="after")
    def check_reservation(self):
        if self.chat_context_window_tokens <= self.chat_max_output_tokens + self.chat_compaction_headroom_tokens:
            raise ValueError("context capacity must leave room for input, output and compaction")
        worst = (16384*self.input_price + self.max_output_tokens*self.output_price + 999999)//1000000
        if self.cost_limits_enabled and self.enabled and self.task_cost_microunits and worst > self.task_cost_microunits:
            raise ValueError("token price ceiling exceeds request budget")
        if self.enabled and self.chat_enabled and self.task_cost_microunits and self.chat_output_limit() < 32:
            raise ValueError("chat token price ceiling exceeds request budget")
        return self
