/* Host side of the optional transcript block-session channel.

   A generation names one loaded frame document. Capability negotiation,
   app-reported state and owner events all belong to exactly one generation:
   a reload or version promotion starts a new one, and nothing negotiated by
   an earlier document is reused. Generations grow across every canvas on the
   page, so a stale value, even from a remounted canvas, can never match again. */

let latestGeneration = 0

export function createBlockDocuments() {
  const documents = new Map()
  return {
    /** A frame's onLoad: always a new document, even for the same version. */
    load(version) {
      const document = { generation: ++latestGeneration, supported: null }
      documents.set(version, document)
      return document
    },
    /** The document's own frame-mounted report. */
    mounted(version, supported) {
      const document = documents.get(version)
      if (document) document.supported = supported === true
      return document || null
    },
    get(version) { return documents.get(version) || null },
    delete(version) { documents.delete(version) },
  }
}

/** The session envelope a document receives, stamped with its generation. */
export function blockSessionEnvelope(session, generation) {
  if (!session || !Number.isSafeInteger(generation)) return null
  return { sessionId: session.sessionId, generation, actions: session.actions,
    checkpoint: session.checkpoint ?? null, retain: session.retain === true,
    recoveryError: session.recoveryError ?? null }
}

/** Whether app-reported state from `document` may reach the block host. */
export function acceptsBlockState({ message, session, document, isLive }) {
  return Boolean(isLive && session && document?.supported === true
    && message?.sessionId === session.sessionId)
}

/** The owner event to post to the live document, once per nonce. */
export function blockEventDelivery({ session, event, document, sentNonce }) {
  if (!session || !event || document?.supported !== true) return null
  if (event.sessionId !== session.sessionId || event.generation !== document.generation) return null
  if (event.nonce === sentNonce) return null
  return { type: 'moebius:app-block-action', sessionId: event.sessionId, generation: event.generation,
    key: event.key, event: event.event, nonce: event.nonce }
}

/** A host request is honoured only from the live, visible document. Hidden
 *  passive sessions and buffered successors mount without an owner gesture. */
export function hostRequestAllowed({ sourceVersion, liveVersion, visible }) {
  return sourceVersion === liveVersion && visible === true
}
