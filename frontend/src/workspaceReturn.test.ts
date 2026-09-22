import { describe, expect, it } from 'vitest'
import {
  appendWorkspaceOrigin,
  copyWorkspaceReturn,
  isFromWorkQueue,
  workspaceReturnLabel,
  workspaceReturnPath,
} from './workspaceReturn'

describe('workspaceReturnPath', () => {
  it('returns Work Queue filters when opened from New Assignments', () => {
    const search = new URLSearchParams({
      from: 'work-queue',
      client_id: '2',
      queue: 'type=new&client_id=2&page=0',
    })
    expect(workspaceReturnPath(search)).toBe('/work-queue?type=new&client_id=2&page=0')
    expect(workspaceReturnLabel(search)).toBe('← Back to New Assignments')
  })

  it('returns Work Queue when opened from due work', () => {
    const search = new URLSearchParams({
      from: 'work-queue',
      queue: 'type=work-next',
    })
    expect(workspaceReturnPath(search)).toBe('/work-queue?type=work-next')
    expect(workspaceReturnLabel(search)).toBe('← Back to Work Queue')
  })

  it('returns Prospects with list state', () => {
    const search = new URLSearchParams({
      from: 'prospects',
      list: 'status=New&q=bison',
    })
    expect(workspaceReturnPath(search)).toBe('/prospects?status=New&q=bison')
    expect(workspaceReturnLabel(search)).toBe('← Back to Prospects')
  })

  it('returns Dashboard for header search origin', () => {
    const search = new URLSearchParams({ from: 'search' })
    expect(workspaceReturnPath(search)).toBe('/')
  })

  it('does not use browser-history guessing as the default', () => {
    expect(workspaceReturnPath(new URLSearchParams())).toBe('/prospects')
  })
})

describe('copyWorkspaceReturn', () => {
  it('keeps queue context on SAVE & NEXT navigation', () => {
    const current = new URLSearchParams({
      from: 'work-queue',
      queue: 'type=new&client_id=3',
      client_id: '3',
    })
    expect(copyWorkspaceReturn('/companies/RN-2?client_id=3', current)).toBe(
      '/companies/RN-2?client_id=3&from=work-queue&queue=type%3Dnew%26client_id%3D3',
    )
  })
})

describe('appendWorkspaceOrigin', () => {
  it('tags a Prospects-origin company link', () => {
    expect(
      appendWorkspaceOrigin('/companies/RN-1?client_id=2', {
        from: 'prospects',
        listQuery: 'q=smith',
      }),
    ).toBe('/companies/RN-1?client_id=2&from=prospects&list=q%3Dsmith')
  })
})

describe('isFromWorkQueue', () => {
  it('detects the Work Queue origin flag', () => {
    expect(isFromWorkQueue('work-queue')).toBe(true)
    expect(isFromWorkQueue('prospects')).toBe(false)
  })
})
