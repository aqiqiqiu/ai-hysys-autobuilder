from __future__ import annotations

import logging
import json
import os
import re
from typing import Iterable, List, Tuple

from models import ReactorSelection, ReactorType, ScenarioSpec


class NaturalLanguageReactorSelector:
    """
    基于大模型的自然语言反应器选型器（阿里云百炼兼容 OpenAI API）。

    - 输出结构化 JSON（见 `ReactorSelection`）
    - 保留工程化日志与异常可诊断性
    - 支持流式输出思考过程与最终回答（写入日志）
    """

    def select(self, scenario: ScenarioSpec) -> ReactorSelection:
        text = (scenario.input_text or "").strip()
        if not text:
            raise ValueError("Scenario input_text is empty; cannot select reactor type.")

        logger = logging.getLogger("ai_hysys_autobuilder")

        model_json, reasoning_text, answer_text = self._select_with_llm_json(scenario, logger=logger)
        reactor_type, confidence, rationale = self._validate_and_normalize_model_json(model_json)
        suggested_hysys = self._suggest_hysys_defaults(reactor_type, scenario)

        # Keep some auditability without changing return shape.
        if reasoning_text:
            logger.debug("LLM reasoning (streamed) captured, length=%d", len(reasoning_text))
        if answer_text:
            logger.debug("LLM answer (streamed) captured, length=%d", len(answer_text))

        return ReactorSelection(
            scenario_id=scenario.scenario_id,
            scenario_name=scenario.name,
            input_text=scenario.input_text,
            reactor_type=reactor_type,
            confidence=float(confidence),
            rationale=list(rationale),
            suggested_hysys=suggested_hysys,
        )

    def selection_json(self, selection: ReactorSelection, indent: int = 2) -> str:
        return json.dumps(selection.to_json_dict(), ensure_ascii=False, indent=indent)

    # -----------------------------
    # LLM selection
    # -----------------------------

    def _select_with_llm_json(
        self, scenario: ScenarioSpec, logger: logging.Logger
    ) -> tuple[dict, str, str]:
        """
        Returns:
            (model_json_dict, reasoning_text, answer_text)

        Notes:
        - Streams reasoning + answer to logs.
        - The model is instructed to output ONLY a JSON object as the final answer.
        """
        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "Missing dependency 'openai'. Install it (e.g. `pip install openai`) to enable LLM reactor selection."
            ) from e

        api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "Environment variable DASHSCOPE_API_KEY is not set. "
                "Set it to your DashScope (阿里云百炼) API key to enable LLM reactor selection."
            )

        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        conversion_hint = scenario.conversion_fraction
        if conversion_hint is None:
            conversion_hint = self._parse_conversion(scenario.input_text or "")

        system_prompt = (
            "你是资深化工过程模拟工程师，任务是在 Aspen HYSYS 里为用户描述的反应体系选择反应器模型类型。\n"
            "你必须在三者中选择其一：Conversion / Equilibrium / Gibbs。\n"
            "输出必须是严格 JSON（不要 Markdown、不要解释性文本、不要前后缀），结构为：\n"
            '{"reactor_type":"Conversion|Equilibrium|Gibbs","confidence":0.0,"rationale":["理由1","理由2"]}\n'
            "其中 confidence 为 0~1 的浮点数，rationale 至少 2 条，语言用中文。\n"
            "选择原则（简要）：\n"
            "- Conversion：已给定单/多反应转化率或目标转化率，偏工艺考核/目标驱动。\n"
            "- Equilibrium：已知反应集合，按化学平衡常数/平衡反应求解。\n"
            "- Gibbs：反应路径复杂/反应集合不完备/希望由元素守恒+自由能最小化自动分配平衡产物。\n"
            "如果信息不足，优先 Gibbs，但降低 confidence 并在 rationale 中说明需要补充哪些信息。"
        )

        user_payload = {
            "scenario_id": scenario.scenario_id,
            "scenario_name": scenario.name,
            "input_text": scenario.input_text,
            "property_package": scenario.property_package,
            "temperature_c": scenario.temperature_c,
            "pressure_kpa": scenario.pressure_kpa,
            "conversion_fraction_hint": conversion_hint,
        }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

        completion = client.chat.completions.create(
            model="deepseek-v3.2",
            messages=messages,
            extra_body={"enable_thinking": True},
            stream=True,
            stream_options={"include_usage": True},
        )

        reasoning_content = ""
        answer_content = ""
        is_answering = False

        logger.info("LLM reactor selection started (streaming enabled).")
        for chunk in completion:
            if not getattr(chunk, "choices", None):
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    logger.info("LLM usage: %s", usage)
                continue

            delta = chunk.choices[0].delta

            rc = getattr(delta, "reasoning_content", None)
            if rc is not None:
                reasoning_content += rc
                # Stream reasoning to logs (typically hidden unless DEBUG)
                logger.debug("%s", rc)

            c = getattr(delta, "content", None)
            if c:
                if not is_answering:
                    is_answering = True
                    logger.info("LLM final answer streaming started.")
                answer_content += c
                logger.info("%s", c)

        model_json = self._extract_first_json_object(answer_content)
        return model_json, reasoning_content, answer_content

    def _extract_first_json_object(self, text: str) -> dict:
        """
        Extract the first JSON object from model output and parse it.
        This defends against occasional leading/trailing whitespace or accidental extra tokens.
        """
        if not text or not text.strip():
            raise ValueError("LLM returned empty content; cannot parse reactor selection JSON.")

        s = text.strip()
        # Fast path: direct JSON
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # Robust path: scan for the first balanced {...}
        start = s.find("{")
        if start < 0:
            raise ValueError(f"LLM output does not contain JSON object: {s!r}")

        depth = 0
        for i in range(start, len(s)):
            ch = s[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except Exception as e:
                        raise ValueError(f"Failed to parse LLM JSON: {candidate!r}") from e
                    if not isinstance(obj, dict):
                        raise ValueError(f"LLM JSON must be an object/dict, got: {type(obj)}")
                    return obj

        raise ValueError("Unbalanced JSON braces in LLM output; cannot parse reactor selection JSON.")

    def _validate_and_normalize_model_json(self, obj: dict) -> tuple[ReactorType, float, List[str]]:
        reactor_type_raw = obj.get("reactor_type")
        if not isinstance(reactor_type_raw, str) or not reactor_type_raw.strip():
            raise ValueError("LLM JSON missing required string field 'reactor_type'.")

        rt = reactor_type_raw.strip().lower()
        if rt == "conversion":
            reactor_type = ReactorType.CONVERSION
        elif rt == "equilibrium":
            reactor_type = ReactorType.EQUILIBRIUM
        elif rt == "gibbs":
            reactor_type = ReactorType.GIBBS
        else:
            raise ValueError(
                "LLM JSON field 'reactor_type' must be one of Conversion|Equilibrium|Gibbs."
            )

        confidence_raw = obj.get("confidence")
        try:
            confidence = float(confidence_raw)
        except Exception as e:
            raise ValueError("LLM JSON missing/invalid field 'confidence' (must be number 0~1).") from e
        confidence = max(0.0, min(1.0, confidence))

        rationale_raw = obj.get("rationale")
        if isinstance(rationale_raw, list) and all(isinstance(x, str) for x in rationale_raw):
            rationale = [x.strip() for x in rationale_raw if x.strip()]
        elif isinstance(rationale_raw, str) and rationale_raw.strip():
            # tolerate string rationale
            rationale = [rationale_raw.strip()]
        else:
            rationale = []

        if len(rationale) < 2:
            # enforce minimum 2 items per contract
            rationale = rationale + ["（模型未提供足够理由，建议补充反应机理/转化率/反应集合信息）"]
            rationale = rationale[:2]

        return reactor_type, confidence, rationale

    # -----------------------------
    # Helpers
    # -----------------------------

    def _parse_conversion(self, text: str) -> float | None:
        patterns: List[Tuple[str, float]] = []
        # 60%
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
        if m:
            try:
                val = float(m.group(1)) / 100.0
                return max(0.0, min(1.0, val))
            except ValueError:
                pass

        # "转化率60" (assume percent)
        m = re.search(r"转化率\s*([0-9]+(?:\.[0-9]+)?)", text)
        if m:
            try:
                v = float(m.group(1))
                if v > 1.0:
                    v = v / 100.0
                return max(0.0, min(1.0, v))
            except ValueError:
                pass

        # "0.6"
        m = re.search(r"转化率\s*([01](?:\.[0-9]+)?)", text)
        if m:
            try:
                return max(0.0, min(1.0, float(m.group(1))))
            except ValueError:
                pass
        return None

    def _suggest_hysys_defaults(self, reactor_type: ReactorType, scenario: ScenarioSpec) -> dict:
        # Provide practical defaults; users can override by editing scenario specs.
        base = {
            "property_package": scenario.property_package or "Peng-Robinson",
            "temperature_c": scenario.temperature_c,
            "pressure_kpa": scenario.pressure_kpa,
        }
        if reactor_type == ReactorType.CONVERSION:
            base["conversion_fraction"] = scenario.conversion_fraction or self._parse_conversion(scenario.input_text) or 0.6
        return base
