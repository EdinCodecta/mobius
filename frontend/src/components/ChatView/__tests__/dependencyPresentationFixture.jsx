/* Backend-free mounted production surfaces for dependency lifecycle regressions. */
import React from 'react'
import { createRoot } from 'react-dom/client'
import '../../../index.css'
import '../ChatView.css'
import '../QuestionCard.css'
import '../../Drawer/Drawer.css'
import GoalPlanDetails from '../GoalPlanDetails.jsx'
import QuestionCard from '../QuestionCard.jsx'
import WaitHistoryCard from '../WaitHistoryCard.jsx'
import ErrorCard from '../ErrorCard.jsx'
import DrawerStatusMarker from '../../Drawer/DrawerStatusMarker.jsx'
const chats = [{ chat_id: 'dependent', title: 'Finish the blocked update' }, { chat_id: 'launch', title: 'Prepare the launch' }]
const dependency = { id: 'd', target_kind: 'owner_card', condition: 'Choose the exact release to install after preserving local work and checking the host', source_chat_id: 'source', source_question_id: 'choice', source_chat_title: 'Restart and update recovery', automatic: true }
window.navigations = []
window.resumes = 0
const navigate = url => window.navigations.push(url.href)
function Fixture({ held = false } = {}) {
  return <main style={{ maxWidth: 680, margin: '24px auto', padding: 16, fontFamily: 'var(--font)', color: 'var(--text)' }}>
    <h2>Goal handoffs</h2>
    <div style={{ display: 'flex', gap: 20, margin: '16px 0' }}>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><DrawerStatusMarker waiting />Waiting</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><DrawerStatusMarker onHold />On hold</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><DrawerStatusMarker ownerRequired />Your input</span>
    </div>
    <QuestionCard chatId="source" questionId="choice" questions={[{ question: 'Which release should we install?', options: [{ id: 'later', label: 'Not now' }] }]} neededBy={chats} onInternalNav={navigate} />
    <section aria-label="Dependent Goal"><GoalPlanDetails dependencies={[{ ...dependency, automatic: !held, resume_blocker: held ? 'goal_held' : null }]} onInternalNav={navigate} /></section>
    <WaitHistoryCard summary={{ ...dependency, disposition: 'delivered', result_disposition: 'delivered', received_run_id: 'received', outcome: { kind: 'closed_without_reply', answers: { release: 'Not now' } } }} />
    <ErrorCard block={{ code: 'goal_settlement_unfinished', resumable: true }}>
      <button className="chat__resume chat__recovery-action" onClick={() => { window.resumes++ }}>Resume</button>
    </ErrorCard>
  </main>
}
const root = createRoot(document.getElementById('root'))
window.mountFixture = props => root.render(<Fixture {...props} />)
window.mountFixture({})
