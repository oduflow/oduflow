// The model picker: ACP agents report models either as `models` (legacy) or
// as a `model` config option (current spec). Same reading as static/chat.js.

export interface ModelChoice {
  modelId: string;
  name: string;
}

export interface Models {
  availableModels: ModelChoice[];
  currentModelId: string;
}

interface ConfigOption {
  id?: string;
  category?: string;
  name?: string;
  value?: unknown;
  currentValue?: unknown;
  options?: ConfigOption[];
}

function flatten(options: unknown, result: ModelChoice[]): ModelChoice[] {
  if (!Array.isArray(options)) return result;
  for (const option of options as ConfigOption[]) {
    if (!option) continue;
    if (Array.isArray(option.options)) {
      flatten(option.options, result);
    } else if (option.value != null) {
      result.push({ modelId: String(option.value), name: option.name || String(option.value) });
    }
  }
  return result;
}

export function sessionModels(res: unknown): Models | null {
  if (!res || typeof res !== 'object') return null;
  const result = res as { models?: Models; configOptions?: unknown };
  if (result.models) {
    const models = result.models;
    if (!Array.isArray(models.availableModels) || !models.availableModels.length) return null;
    return {
      availableModels: models.availableModels.map((m) => ({
        modelId: m.modelId,
        name: m.name || m.modelId,
      })),
      currentModelId: models.currentModelId,
    };
  }
  if (!Array.isArray(result.configOptions)) return null;
  for (const option of result.configOptions as ConfigOption[]) {
    if (!option || (option.id !== 'model' && option.category !== 'model')) continue;
    const available = flatten(option.options || [], []);
    if (!available.length) return null;
    return { availableModels: available, currentModelId: String(option.currentValue || '') };
  }
  return null;
}
