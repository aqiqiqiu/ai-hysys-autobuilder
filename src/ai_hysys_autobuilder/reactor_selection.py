from __future__ import annotations

import logging
import json
import os
import re
from typing import List, Tuple, Optional

from models import ReactorSelection, ReactorType, ScenarioSpec


class NaturalLanguageReactorSelector:
    """
    基于大模型的自然语言反应器选型器（阿里云百炼兼容 OpenAI API）。
    大模型会输出 reactor_type, confidence, rationale, 以及从文本中推断的
    temperature_c, pressure_kpa, conversion_fraction 等参数。
    """

    def select(self, scenario: ScenarioSpec) -> ReactorSelection:
        text = (scenario.input_text or "").strip()
        if not text:
            raise ValueError("Scenario input_text is empty; cannot select reactor type.")

        logger = logging.getLogger("ai_hysys_autobuilder")

        model_json, reasoning_text, answer_text = self._select_with_llm_json(scenario, logger=logger)
        (
            reactor_type,
            confidence,
            rationale,
            temperature_c,
            pressure_kpa,
            conversion_fraction,
        ) = self._validate_and_normalize_model_json(model_json)

        suggested_hysys = {
            "property_package": scenario.property_package or "Peng-Robinson",
            "temperature_c": temperature_c if temperature_c is not None else scenario.temperature_c,
            "pressure_kpa": pressure_kpa if pressure_kpa is not None else scenario.pressure_kpa,
            "conversion_fraction": conversion_fraction if conversion_fraction is not None else scenario.conversion_fraction,
        }

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
        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:
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

        system_prompt = (
            "你是一个资深化工过程模拟工程师。你的任务是根据用户对反应体系的自然语言描述，做出判断并输出 JSON。\n"
            "输出必须是一个严格的 JSON 对象（不要任何额外文本），结构如下：\n"
            "{\n"
            '  "reactor_type": "Conversion" or "Equilibrium" or "Gibbs",\n'
            '  "confidence": 0.0~1.0 的浮点数,\n'
            '  "rationale": ["理由1", "理由2"],\n'
            '  "temperature_c": 从描述中推断的反应温度（摄氏度），没有则 null,\n'
            '  "pressure_kpa": 从描述中推断的反应压力（千帕），没有则 null,\n'
            '  "conversion_fraction": 如果反应器类型是 Conversion，从描述中推断的转化率（0~1），否则 null\n'
            "}\n"
            "选择原则：\n"
            "- Conversion：用户明确给出了单/多反应的转化率或目标转化率。\n"
            "- Equilibrium：用户给出了可逆反应或平衡反应信息（如反应平衡常数、可逆符号等）。\n"
            "- Gibbs：反应路径复杂、副反应多、产物分布未知或温度极高（>800°C）等。\n"
            "如果信息不足，优先 Gibbs，但降低 confidence 并在 rationale 中说明缺少哪些信息。\n"
            "请只输出上述 JSON，不要有其他内容。"
        )

        user_payload = {
            "scenario_id": scenario.scenario_id,
            "scenario_name": scenario.name,
            "input_text": scenario.input_text,
        }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

        # 使用非流式请求，避免流式解析问题
        completion = client.chat.completions.create(
            model="deepseek-v3.2",
            messages=messages,
            extra_body={"enable_thinking": True},
            stream=False,
        )

        answer_content = completion.choices[0].message.content
        model_json = self._extract_first_json_object(answer_content)
        return model_json, "", answer_content

    def _extract_first_json_object(self, text: str) -> dict:
        if not text or not text.strip():
            raise ValueError("LLM returned empty content; cannot parse reactor selection JSON.")

        s = text.strip()
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

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
                        if isinstance(obj, dict):
                            return obj
                    except Exception as e:
                        raise ValueError(f"Failed to parse LLM JSON: {candidate!r}") from e
                    break
        raise ValueError("Unbalanced JSON braces in LLM output; cannot parse reactor selection JSON.")

    def _validate_and_normalize_model_json(
        self, obj: dict
    ) -> tuple[ReactorType, float, List[str], Optional[float], Optional[float], Optional[float]]:
        # reactor_type
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
            raise ValueError("LLM JSON field 'reactor_type' must be one of Conversion|Equilibrium|Gibbs.")

        # confidence
        confidence_raw = obj.get("confidence")
        try:
            confidence = float(confidence_raw)
        except Exception:
            raise ValueError("LLM JSON missing/invalid field 'confidence' (must be number 0~1).")
        confidence = max(0.0, min(1.0, confidence))

        # rationale
        rationale_raw = obj.get("rationale")
        if isinstance(rationale_raw, list) and all(isinstance(x, str) for x in rationale_raw):
            rationale = [x.strip() for x in rationale_raw if x.strip()]
        elif isinstance(rationale_raw, str) and rationale_raw.strip():
            rationale = [rationale_raw.strip()]
        else:
            rationale = []
        if len(rationale) < 2:
            rationale = rationale + ["（模型未提供足够理由，建议补充反应信息）"]
            rationale = rationale[:2]

        # temperature_c
        temp_raw = obj.get("temperature_c")
        temperature_c = None
        if temp_raw is not None:
            try:
                temperature_c = float(temp_raw)
            except Exception:
                pass

        # pressure_kpa
        press_raw = obj.get("pressure_kpa")
        pressure_kpa = None
        if press_raw is not None:
            try:
                pressure_kpa = float(press_raw)
            except Exception:
                pass

        # conversion_fraction
        conv_raw = obj.get("conversion_fraction")
        conversion_fraction = None
        if conv_raw is not None:
            try:
                conversion_fraction = float(conv_raw)
                conversion_fraction = max(0.0, min(1.0, conversion_fraction))
            except Exception:
                pass

        return reactor_type, confidence, rationale, temperature_c, pressure_kpa, conversion_fraction

    # -----------------------------
    # Helpers (保留 parse_conversion 用于后备)
    # -----------------------------
    def _parse_conversion(self, text: str) -> float | None:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
        if m:
            val = float(m.group(1)) / 100.0
            return max(0.0, min(1.0, val))
        m = re.search(r"转化率\s*([0-9]+(?:\.[0-9]+)?)", text)
        if m:
            v = float(m.group(1))
            if v > 1.0:
                v /= 100.0
            return max(0.0, min(1.0, v))
        m = re.search(r"([01](?:\.[0-9]+)?)", text)
        if m:
            return max(0.0, min(1.0, float(m.group(1))))
        return None