# Tether

This is my first commit


# Tether

## Project Overview

Tether is a hyper-local career agent built for Monroe, Louisiana — matching ULM and BPCC graduates with real employers in the Ouachita Parish region so they can build careers without leaving home. It combines curated employer data, skills-gap analysis, and AI-driven guidance to surface the right opportunity for each candidate based on their degree, skill set, and salary expectations. Unlike generic job boards, Tether is opinionated: it knows the local market, speaks the language of northeast Louisiana industries, and keeps its recommendations anchored to employers who are actually hiring here.

This is an AI-driven job finder application that personalizes job discovery using intelligent systems to understand user profiles, preferences, and skills. The platform features a web interface for user interaction and a backend API for processing requests, including resume analysis, job matching, and automated outreach.

## Technologies Used

### Backend
- **Python** - Core programming language
- **FastAPI** - Modern, fast web framework for building APIs
- **Uvicorn** - ASGI server for running FastAPI applications
- **Anthropic Claude** - AI model for intelligent job matching and analysis
- **Pydantic** - Data validation and settings management
- **PyPDF** - PDF processing for resume uploads
- **python-jobspy** - Job scraping and data collection
- **SlowAPI** - Rate limiting for API endpoints
- **python-dotenv** - Environment variable management

### Frontend
- **Next.js 14** - React framework for production
- **React 18** - UI library
- **TypeScript** - Type-safe JavaScript
- **Tailwind CSS** - Utility-first CSS framework
- **shadcn/ui** - Component library built on Radix UI
- **Framer Motion** - Animation library
- **Axios** - HTTP client for API requests
- **Lucide React** - Icon library

### Other Tools
- **Git** - Version control
- **VS Code** - Development environment

## Setup and Run Instructions

### Prerequisites
- Python 3.8 or higher
- Node.js 18 or higher
- npm or yarn package manager

### Backend Setup
1. Navigate to the backend directory:
   ```bash
   cd backend
   ```

2. Create a virtual environment:
   ```bash
   python -m venv .venv
   ```

3. Activate the virtual environment:
   ```bash
   source .venv/bin/activate  # On macOS/Linux
   # or
   .venv\Scripts\activate     # On Windows
   ```

4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

5. Set up environment variables:
   Create a `.env` file in the backend directory with necessary API keys (e.g., Anthropic API key).

6. Run the backend server:
   ```bash
   uvicorn main:app --reload
   ```
   The API will be available at `http://localhost:8000`.

### Frontend Setup
1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Run the development server:
   ```bash
   npm run dev
   ```
   The frontend will be available at `http://localhost:3000`.

### Running the Full Application
1. Start the backend server as described above.
2. In a separate terminal, start the frontend server.
3. Open your browser and navigate to `http://localhost:3000` to access the application.

## Basic Architecture and Workflow

### Architecture
Tether follows a client-server architecture:

- **Frontend**: A Next.js web application providing the user interface with components for authentication, dashboard, chat interface, automator panel, outreach panel, and report generation.

- **Backend**: A FastAPI-based REST API handling business logic, including:
  - Authentication and user management
  - Job data processing and matching
  - AI-powered resume analysis and recommendations
  - Automated outreach and communication
  - Report generation

- **Database**: SQLite-based data storage for user data, employer information, and job listings.

### Workflow
1. **User Registration/Login**: Users create accounts and log in through the frontend.

2. **Profile Setup**: Users upload resumes and provide preferences, which are processed by the backend.

3. **Job Matching**: The AI analyzes user profiles against curated employer data to suggest relevant opportunities.

4. **Interaction**: Users can engage with the chat interface for guidance, use the automator for streamlined applications, and generate reports on their progress.

5. **Outreach**: Automated tools help users connect with employers in the local market.

This architecture ensures a seamless, AI-enhanced experience tailored to the Monroe, Louisiana job market.
