import { useEffect } from 'react'
import { registerNativePush } from '../lib/nativePush.js'

/** Registers this phone for native push inside the Möbius mobile app. */
export default function useNativePush() {
  useEffect(() => {
    registerNativePush().catch(() => {})
  }, [])
}
