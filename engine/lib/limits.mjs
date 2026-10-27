// 4c-4: the single source of output limits is limits.json (Python limits.py reads the same file).
import { readFileSync } from 'node:fs';

export const LIMITS = JSON.parse(readFileSync(new URL('./limits.json', import.meta.url), 'utf8'));

// Role: the engine's phase role (CADRE_ROLE) or the plan agent; every other call gets the lane cap. On the single attempt after a truncation
// the engine sets CADRE_OUTPUT_MULTIPLIER=2; the caller clips the result to the model's own cap.
export function roleOutput(role, agent) {
  const output = LIMITS.output;
  const multiplier = process.env.CADRE_OUTPUT_MULTIPLIER === '2' ? 2 : 1;
  if (agent === 'plan' || agent === 'cadre-plan' || role === 'plan') return output.plan * multiplier;
  return (output[role] ?? output.lane) * multiplier;
}

// Visible output request: with Anthropic budget-type thinking max_tokens includes the thinking budget too; the gate measures the difference (16-4).
export function requestedOutput(body) {
  const total = body.max_completion_tokens ?? body.max_tokens ?? body.max_output_tokens ?? body.generationConfig?.maxOutputTokens;
  const budget = body.thinking?.type === 'enabled' ? body.thinking.budget_tokens : 0;
  if (!Number.isSafeInteger(total)) return { total, visible: total };
  return { total, visible: Number.isSafeInteger(budget) && budget > 0 && budget < total ? total - budget : total };
}
