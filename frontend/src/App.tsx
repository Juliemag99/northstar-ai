import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { DashboardPage } from './pages/DashboardPage'
import { PlaceholderPage } from './pages/PlaceholderPage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route path="prospects" element={<PlaceholderPage />} />
          <Route path="companies" element={<PlaceholderPage />} />
          <Route path="contacts" element={<PlaceholderPage />} />
          <Route path="activities" element={<PlaceholderPage />} />
          <Route path="appointments" element={<PlaceholderPage />} />
          <Route path="campaigns" element={<PlaceholderPage />} />
          <Route path="tasks" element={<PlaceholderPage />} />
          <Route path="reports" element={<PlaceholderPage />} />
          <Route path="research" element={<PlaceholderPage />} />
          <Route path="clients" element={<PlaceholderPage />} />
          <Route path="administration" element={<PlaceholderPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
