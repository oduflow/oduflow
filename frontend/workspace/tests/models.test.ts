import { describe, expect, it } from 'vitest';
import { sessionModels } from '../src/lib/models';

describe('models', () => {
  it('reads the legacy models field', () => {
    expect(
      sessionModels({ models: { availableModels: [{ modelId: 'a', name: 'A' }], currentModelId: 'a' } })
    ).toEqual({ availableModels: [{ modelId: 'a', name: 'A' }], currentModelId: 'a' });
  });

  it('reads a grouped model config option', () => {
    expect(
      sessionModels({
        configOptions: [
          { id: 'mode', options: [{ value: 'x' }] },
          { id: 'model', currentValue: 'b', options: [{ options: [{ value: 'a', name: 'A' }, { value: 'b' }] }] },
        ],
      })
    ).toEqual({
      availableModels: [
        { modelId: 'a', name: 'A' },
        { modelId: 'b', name: 'b' },
      ],
      currentModelId: 'b',
    });
  });

  it('returns null without models', () => {
    expect(sessionModels({})).toBeNull();
    expect(sessionModels(null)).toBeNull();
  });
});
