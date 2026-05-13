"""Claude-based strength calculation for levels."""

import json
import anthropic
from config import CLAUDE_API_KEY
from constants import CLAUDE_MODEL, CLAUDE_MAX_TOKENS
from analysis.chart_ascii import generate_ascii_chart, generate_levels_summary
from logger import logger

client = anthropic.AsyncAnthropic(api_key=CLAUDE_API_KEY)


async def calculate_strength_with_claude(symbol: str, c15m: list[dict], levels: list[dict], poc_price: float = None) -> list[dict]:
    """
    Use Claude Haiku to analyze levels and determine their strength.
    """
    if not levels:
        return levels
    
    # Generate ASCII chart
    chart = generate_ascii_chart(c15m, levels, poc_price, symbol=symbol)
    
    # Calculate average candle volume for relative comparison
    avg_volume = sum(c["volume"] for c in c15m[-50:]) / min(len(c15m), 50) if c15m else 0
    
    # Generate levels summary
    summary = generate_levels_summary(levels, poc_price, avg_volume=avg_volume)
    
    # Log what Claude actually sees per level
    logger.info("Claude input levels",
                symbol=symbol,
                levels=[{
                    "price": l["level"],
                    "candle_count": l.get("candle_count", 0),
                    "hourly_open_bonus": l.get("hourly_open_bonus", 0),
                    "round_number_bonus": l.get("round_number_bonus", 0),
                    "poc_aligned": l.get("poc_aligned", False),
                    "position": l.get("position", "?"),
                    "volume_at_level": round(l.get("volume_at_level", 0), 0),
                } for l in levels])
    
    # Build prompt for Claude
    prompt = f"""Ты профессиональный трейдер-аналитик. Проанализируй график {symbol} и определи силу каждого уровня поддержки.

{chart}

{summary}

КРИТЕРИИ СИЛЫ (1-5 звезд):
⭐⭐⭐⭐⭐ (5 звезд) - ОЧЕНЬ СИЛЬНЫЙ:
- Выравнивание с POC (максимальный объем) - ЭТО САМЫЙ ВАЖНЫЙ ФАКТОР
- Много касаний (5+)
- Открытие на 4h (редкое событие)
- Близко к круглому числу
- У начала пампа

⭐⭐⭐⭐ (4 звезды) - СИЛЬНЫЙ:
- Хорошие касания (3-4)
- Есть выравнивание по таймфрейму
- Приличный объем

⭐⭐⭐ (3 звезды) - СРЕДНИЙ:
- Мало касаний (2-3)
- Нет особых признаков

⭐⭐ (2 звезды) - СЛАБЫЙ:
- Очень мало касаний (1-2)
- Далеко от ключевых уровней

⭐ (1 звезда) - ОЧЕНЬ СЛАБЫЙ:
- Одно касание
- Нет подтверждения

ВАЖНЫЕ ПРАВИЛА:
1. POC (Point of Control) - САМЫЙ ВАЖНЫЙ уровень с максимальным объемом
2. Если уровень выравнен с POC - он ОБЯЗАН получить 5 звезд
3. Только ОДИН уровень может быть выравнен с POC (помечен "YES - MAXIMUM VOLUME")
4. Каждый уровень должен иметь УНИКАЛЬНОЕ обоснование
5. НЕ перечисляй ВСЕ признаки - выбери 1-2 ГЛАВНЫХ отличия
6. Если у уровней одинаковые признаки - найди РАЗНИЦУ (количество касаний, позиция, объем)
7. Будь КРАТКИМ - максимум 8-10 слов на причину
8. 4h важнее 1h, но упоминай только если это главное отличие

ПРИМЕРЫ ХОРОШИХ ПРИЧИН:
✅ "POC с максимальным объемом"
✅ "Больше всего касаний (8)"
✅ "Открытие 4h, близко к 0.033"
✅ "Средний объем, 3 касания"

ПРИМЕРЫ ПЛОХИХ ПРИЧИН:
❌ "Highest touch count (6 touches), 4h open alignment, very close to round number" (длинно, перечисление)
❌ "4h open alignment, very close to round number" (повторяется)

ОТВЕЧАЙ ТОЛЬКО НА РУССКОМ!

Верни ТОЛЬКО JSON:
{{
  "levels": [
    {{"price": 0.028968, "strength": 5, "reason": "POC с максимальным объемом"}},
    {{"price": 0.032952, "strength": 4, "reason": "Больше всего касаний (6)"}},
    {{"price": 0.033938, "strength": 4, "reason": "5 касаний, близко к 0.034"}},
    {{"price": 0.034272, "strength": 3, "reason": "3 касания, средний объем"}}
  ]
}}"""

    try:
        # Call Claude Haiku
        logger.debug("Calling Claude Haiku for strength analysis",
                    symbol=symbol,
                    levels_count=len(levels))
        
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        
        response_text = response.content[0].text
        
        # Parse JSON response
        # Remove markdown code blocks if present
        response_text = response_text.strip()
        if response_text.startswith("```"):
            # Extract JSON from markdown
            lines = response_text.split("\n")
            json_lines = []
            in_code = False
            for line in lines:
                if line.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code or (not line.startswith("```")):
                    json_lines.append(line)
            response_text = "\n".join(json_lines)
        
        result = json.loads(response_text)
        
        # Update levels with Claude's strength
        claude_levels = {lvl["price"]: lvl for lvl in result.get("levels", [])}
        
        for lvl in levels:
            price = lvl["level"]
            
            # Find matching Claude analysis (with tolerance)
            claude_data = None
            for claude_price, data in claude_levels.items():
                if price > 0 and abs(claude_price - price) / price < 0.005:
                    claude_data = data
                    break
            
            if claude_data:
                lvl["strength"] = claude_data["strength"]
                lvl["claude_reason"] = claude_data["reason"]
                lvl["verdict"] = "hold"  # Default verdict
                
                logger.info("Claude strength assigned",
                           symbol=symbol,
                           level=price,
                           strength=claude_data["strength"],
                           reason=claude_data["reason"])
            else:
                # Fallback to default
                lvl["strength"] = 3
                lvl["claude_reason"] = "Не проанализирован Claude"
                lvl["verdict"] = "hold"
        
        return levels
        
    except Exception as e:
        logger.error("Failed to get Claude strength analysis",
                    symbol=symbol,
                    error=str(e))
        
        # Fallback: use simple logic
        for lvl in levels:
            lvl["strength"] = 3
            lvl["claude_reason"] = f"Ошибка Claude: {str(e)}"
            lvl["verdict"] = "hold"
        
        return levels
