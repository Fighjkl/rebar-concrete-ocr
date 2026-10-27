import request from './request'

export function listPresets() {
  return request({ url: '/api/presets', method: 'get' })
}

export async function getDefaultPreset() {
  try {
    const res = await listPresets()
    if (res && res.data && Array.isArray(res.data.presets) && res.data.presets[0]) {
      return res.data.presets[0]
    }
  } catch (e) {
    // ignore
  }
  return null
}