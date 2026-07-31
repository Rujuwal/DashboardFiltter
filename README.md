# Dashboard Filter - Interview Management System

A comprehensive Flask-based dashboard for managing candidate interviews, team analytics, and recruitment workflows.

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- MongoDB database
- pip (Python package manager)

### Local Setup

1. **Clone the repository**
```bash
git clone <your-repo-url>
cd DashboardFiltter
```

2. **Install dependencies**
```bash
pip install -r requirements.txt
```

3. **Set up environment variables**

Create a `.env` file in the project root:
```bash
MONGO_URI=mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=your_database_name
FLASK_DEBUG=True
FLASK_PORT=5000
```

4. **Run the application**
```bash
python app.py
```

5. **Open your browser**
```
http://localhost:5000
```

## 📁 Project Structure

```
DashboardFiltter/
├── app.py                 # Main application entry point
├── db.py                  # Database connection configuration
├── requirements.txt       # Python dependencies
├── vercel.json           # Vercel deployment configuration
├── routes/               # Application routes
│   ├── dashboard.py      # Dashboard overview
│   ├── candidates.py     # Candidate management
│   ├── teams.py          # Team management
│   └── analytics.py      # Analytics and reporting
├── templates/            # HTML templates
│   ├── base.html
│   ├── interview_records.html
│   ├── interview_stats.html
│   ├── team_analytics.html
│   ├── expert_analytics.html
│   ├── funnel_analytics.html
│   ├── export_center.html
│   ├── search.html
│   └── error.html
└── static/
    └── style.css         # Custom styles
```

## 🌟 Features

### Dashboard Overview
- Real-time candidate statistics
- Interview tracking and status monitoring
- Top experts and teams leaderboard
- Technology distribution analysis
- Funnel conversion metrics
- Recent activity feed
- Monthly trends visualization

### Team Assignment
- Team and team-lead mapping is derived directly from `users.teamLead`
  (falling back to `TeamLead` / `Team Lead` / `team`) in the main MongoDB
  cluster -- no separate teams database to manage.

### Analytics
- **Expert Analytics**: Individual expert performance metrics, plus live
  active-candidate caseload (count + top candidate technologies)
- **Team Analytics**: Team-based interview statistics
- **Candidate Analytics**: Candidate funnel performance
- **Funnel Analytics**: Conversion rates across interview stages
- **Export Center**: Download data in Excel format

### Interview Management
- View all interview records
- Filter by status, date, expert, or team
- Track scheduling and rescheduling
- Monitor cancellations and completion rates

## 🔧 Configuration

### Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `MONGO_URI` | Yes | MongoDB connection string | `mongodb+srv://...` |
| `MONGO_DB` | Yes | Main database name | `interview_db` |
| `FLASK_DEBUG` | No | Enable debug mode (default: True) | `False` |
| `FLASK_PORT` | No | Application port (default: 5000) | `8080` |

## 🚀 Deployment

### Vercel Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed deployment instructions.

**Quick steps:**

1. Connect your GitHub repository to Vercel
2. Set environment variables in Vercel dashboard
3. Deploy!

**Important:** Make sure to set `MONGO_URI` and `MONGO_DB` in Vercel environment variables, or you'll get a 500 error.

### Health Check

After deployment, verify the application is running:

```
GET https://your-app.vercel.app/health
```

Expected response:
```json
{
  "status": "healthy",
  "database": "connected"
}
```

## 📊 Database Collections

### candidateDetails
Stores candidate information including:
- Personal details
- Technology/skills
- Workflow status
- Branch/location information

### taskBody
Stores interview records including:
- Interview scheduling
- Assigned expert
- Interview round
- Status (Completed, Cancelled, Rescheduled)
- Feedback and notes

### teams
Stores team information including:
- Team name
- Team members (expert names)

## 🛠️ Development

### Running Tests
```bash
# Add your test commands here
python -m pytest
```

### Code Style
```bash
# Format code with black
black .

# Lint with flake8
flake8 .
```

## 🐛 Troubleshooting

### 500 Error on Vercel
- **Cause**: Missing environment variables
- **Solution**: Set `MONGO_URI` and `MONGO_DB` in Vercel settings

### Database Connection Issues
- Check MongoDB Atlas network access settings
- Verify IP whitelist includes Vercel's IPs or 0.0.0.0/0
- Ensure MongoDB credentials are correct

### Import Errors
- Make sure all dependencies are in `requirements.txt`
- Run `pip install -r requirements.txt`

## 📦 Dependencies

- **Flask** (3.0.0): Web framework
- **pymongo** (4.6.1): MongoDB driver
- **python-dotenv** (1.0.0): Environment variable management
- **pandas** (2.1.4): Data manipulation
- **openpyxl** (3.1.2): Excel file generation
- **dnspython** (2.4.2): DNS toolkit for MongoDB

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📝 License

This project is proprietary software for Vizva Consultancy Service.

## 📧 Support

For issues or questions, please contact the development team.

---

**Built with ❤️ by Vizva Consultancy Service**
