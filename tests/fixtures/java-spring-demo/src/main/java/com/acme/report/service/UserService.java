package com.acme.report.service;

import com.acme.report.model.User;
import com.acme.report.repo.UserRepository;
import com.acme.report.support.DateUtils;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class UserService {

    private final UserRepository userRepository;

    public UserService(UserRepository userRepository) {
        this.userRepository = userRepository;
    }

    public User getUser(Long id) {
        User user = userRepository.findById(id).orElse(null);
        if (user != null) {
            user.setLastLogin(DateUtils.now());
        }
        return user;
    }

    public List<User> listUsers() {
        return userRepository.findAll();
    }
}

