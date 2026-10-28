// `location.tracking` capability: lets a visible app start, stop, pace, or
// read the shell's native location controller (lib/nativeLocation.js). The
// controller and `window.MobiusNative` live only in the shell's top frame, so
// this provider is the app's sole route to them. Recorded positions are read
// from the server under the separate `location_history_read` permission.
import * as nativeLocation from './nativeLocation.js'

export const LOCATION_TRACKING = 'location.tracking'

const OPERATIONS = new Set(['get', 'start', 'stop', 'set-options'])

function unavailable() {
  const error = new Error('Location tracking runs only in the Möbius phone app.')
  error.name = 'NotSupportedError'
  error.code = 'unavailable'
  return error
}

function snapshot(controller) {
  const state = controller.getLocationState()
  return {
    available: state.available === true,
    enabled: state.enabled === true,
    status: state.status ?? null,
    options: controller.getTrackingOptions(),
  }
}

export function createLocationTrackingProvider({ controller = nativeLocation } = {}) {
  return {
    version: 1,
    async open({ input, channel }) {
      const operation = input?.operation
      if (!OPERATIONS.has(operation)) {
        throw new TypeError('Unknown location tracking operation.')
      }
      if (operation !== 'get' && !controller.isNativeLocationAvailable()) {
        throw unavailable()
      }
      if (operation === 'start') await controller.startTracking()
      else if (operation === 'stop') await controller.stopTracking()
      else if (operation === 'set-options') {
        const options = input?.options
        if (!options || typeof options !== 'object' || Array.isArray(options)) {
          throw new TypeError('Location tracking options must be an object.')
        }
        await controller.setTrackingOptions(options)
      }
      channel.result(snapshot(controller))
      return {}
    },
  }
}
