import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import Dashboard from './Dashboard.jsx'
import Landing from './Landing.jsx'
import Status from './Status.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/dashboard" element={<Dashboard />} />
        {/* Scanner health + metrics, and the data-mode banner. */}
        <Route path="/status" element={<Status />} />
        {/* Anything else lands on the splash rather than a blank screen. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
