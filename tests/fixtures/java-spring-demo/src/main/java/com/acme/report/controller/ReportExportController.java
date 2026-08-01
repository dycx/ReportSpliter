package com.acme.report.controller;

import com.acme.report.model.Report;
import com.acme.report.service.ReportExportService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/reports")
public class ReportExportController {

    private final ReportExportService reportExportService;

    public ReportExportController(ReportExportService reportExportService) {
        this.reportExportService = reportExportService;
    }

    @GetMapping("/export")
    public Report export(@RequestParam String date,
                         @RequestParam(required = false) String format) {
        return reportExportService.exportReport(date, format);
    }

    @GetMapping("/list")
    public List<Report> list(@RequestParam String date) {
        return reportExportService.listReports(date);
    }
}

