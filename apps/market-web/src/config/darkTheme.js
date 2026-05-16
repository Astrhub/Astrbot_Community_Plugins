const blue = '#2563eb'
const blueHover = '#1d4ed8'
const bluePressed = '#1e40af'

export const lightThemeOverrides = {
  common: {
    duration: '0.2s',
    borderRadius: '6px',
    primaryColor: blue,
    primaryColorHover: blueHover,
    primaryColorPressed: bluePressed,
    baseColor: '#ffffff',
    bodyColor: '#f8fbff',
    cardColor: '#ffffff',
    textColor1: '#0f172a',
    textColor2: '#334155',
    textColor3: '#64748b',
    borderColor: '#dbe7f6'
  },
  Button: {
    borderRadiusMedium: '6px',
    textColorPrimary: '#ffffff',
    textColorHoverPrimary: '#ffffff',
    textColorPressedPrimary: '#ffffff'
  },
  Card: {
    borderRadius: '8px',
    color: '#ffffff',
    colorModal: '#ffffff'
  },
  Tag: {
    borderRadius: '6px'
  },
  Input: {
    borderHover: blue,
    borderFocus: blue
  },
  Select: {
    peers: {
      InternalSelection: {
        borderHover: `1px solid ${blue}`,
        borderActive: `1px solid ${blue}`,
        borderFocus: `1px solid ${blue}`
      }
    }
  }
}

export const darkThemeOverrides = {
  common: {
    duration: '0.2s',
    borderRadius: '6px',
    primaryColor: '#60a5fa',
    primaryColorHover: '#93c5fd',
    primaryColorPressed: '#3b82f6',
    baseColor: '#0f172a',
    bodyColor: '#0b1220',
    cardColor: '#111827',
    textColor1: '#f8fafc',
    textColor2: '#dbeafe',
    textColor3: '#94a3b8',
    borderColor: '#1e3a5f'
  },
  Button: {
    borderRadiusMedium: '6px',
    textColorPrimary: '#0f172a',
    textColorHoverPrimary: '#0f172a',
    textColorPressedPrimary: '#0f172a'
  },
  Card: {
    borderRadius: '8px',
    color: '#111827',
    colorModal: '#111827'
  },
  Tag: {
    borderRadius: '6px'
  },
  Input: {
    color: '#111827',
    textColor: '#f8fafc',
    placeholderColor: '#94a3b8',
    borderHover: '#60a5fa',
    borderFocus: '#60a5fa'
  },
  Select: {
    peers: {
      InternalSelection: {
        textColor: '#f8fafc',
        placeholderColor: '#94a3b8',
        color: '#111827',
        colorActive: '#1f2937',
        border: '1px solid #1e3a5f',
        borderHover: '1px solid #60a5fa',
        borderActive: '1px solid #60a5fa',
        borderFocus: '1px solid #60a5fa'
      },
      InternalSelectMenu: {
        color: '#111827',
        optionTextColor: '#f8fafc',
        optionColorHover: 'rgba(96, 165, 250, 0.18)',
        optionColorActive: 'rgba(96, 165, 250, 0.26)',
        optionTextColorActive: '#f8fafc'
      }
    }
  }
}
