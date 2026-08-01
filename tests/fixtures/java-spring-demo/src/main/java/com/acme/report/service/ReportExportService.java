package com.acme.report.service;

import com.acme.report.model.Report;
import com.acme.report.repo.ReportRepository;
import com.acme.report.support.DateUtils;
import com.acme.report.support.Metrics;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.util.List;

@Service
public class ReportExportService {

    private final ReportRepository reportRepository;
    private final ReportCalculationService calculationService;
    private final PdfRenderer pdfRenderer;

    public ReportExportService(ReportRepository reportRepository,
                               ReportCalculationService calculationService,
                               PdfRenderer pdfRenderer) {
        this.reportRepository = reportRepository;
        this.calculationService = calculationService;
        this.pdfRenderer = pdfRenderer;
    }

    public Report exportReport(String date, String format) {
        LocalDate day = DateUtils.parse(date);
        Report base = reportRepository.findByDate(day);
        Metrics.record("report.export", day);
        Report computed = calculationService.compute(base);
        return pdfRenderer.render(computed, format);
    }

    public List<Report> listReports(String date) {
        LocalDate day = DateUtils.parse(date);
        return reportRepository.findByDateBetween(day, day.plusDays(7));
    }
}

